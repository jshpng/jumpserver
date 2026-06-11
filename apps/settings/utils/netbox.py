# -*- coding: utf-8 -*-
#
import requests
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q

from assets.models import Asset, Host, Node, Platform
from common.utils import get_logger
from labels.models import Label, LabeledResource
from orgs.models import Organization
from orgs.utils import tmp_to_org

logger = get_logger(__name__)

__all__ = [
    'NetBoxClient', 'NetBoxImporter',
    'DEVICE_OBJECT_TYPE', 'VM_OBJECT_TYPE', 'WEBHOOK_MODEL_OBJECT_TYPES',
]

DEVICE_OBJECT_TYPE = 'dcim.device'
VM_OBJECT_TYPE = 'virtualization.virtualmachine'

OBJECT_TYPE_PATHS = {
    DEVICE_OBJECT_TYPE: '/api/dcim/devices/',
    VM_OBJECT_TYPE: '/api/virtualization/virtual-machines/',
}

# NetBox webhook payloads carry a short `model` name instead of the
# dotted object type, map them once here.
WEBHOOK_MODEL_OBJECT_TYPES = {
    'device': DEVICE_OBJECT_TYPE,
    'virtualmachine': VM_OBJECT_TYPE,
}

DELETE_ACTION_SKIP = 'skip'
DELETE_ACTION_DEACTIVATE = 'deactivate'
DELETE_ACTION_DELETE = 'delete'


class NetBoxClient:
    """Thin REST client for the NetBox API (token auth, paginated)."""

    def __init__(self, base_url=None, token=None, verify_ssl=None, timeout=15):
        if base_url is None:
            base_url = settings.NETBOX_BASE_URL
        if token is None:
            token = settings.NETBOX_API_TOKEN
        if verify_ssl is None:
            verify_ssl = settings.NETBOX_VERIFY_SSL
        self.base_url = (base_url or '').rstrip('/')
        self.token = token or ''
        self.verify_ssl = bool(verify_ssl)
        self.timeout = timeout

    @property
    def headers(self):
        return {
            'Authorization': 'Token {}'.format(self.token),
            'Accept': 'application/json',
        }

    def get(self, path, params=None):
        if path.startswith('http://') or path.startswith('https://'):
            url = path
        else:
            url = '{}{}'.format(self.base_url, path)
        resp = requests.get(
            url, params=params, headers=self.headers,
            verify=self.verify_ssl, timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_status(self):
        return self.get('/api/status/')

    def iter_objects(self, path, params=None):
        """Iterate all objects of a list endpoint following pagination."""
        params = dict(params or {})
        params.setdefault('limit', 100)
        url, pages = path, 0
        while url:
            data = self.get(url, params=params)
            # `next` already carries the query string
            params = None
            for item in data.get('results', []):
                yield item
            url = data.get('next')
            pages += 1
            if pages >= 10000:
                logger.error('NetBox pagination exceeded 10000 pages, abort')
                break

    def iter_objects_of_type(self, object_type, statuses=None):
        path = OBJECT_TYPE_PATHS[object_type]
        params = {}
        if statuses:
            params['status'] = list(statuses)
        return self.iter_objects(path, params=params)

    def get_object(self, object_type, object_id):
        path = '{}{}/'.format(OBJECT_TYPE_PATHS[object_type], object_id)
        return self.get(path)


class NetBoxImporter:
    """
    Map NetBox devices / virtual machines onto JumpServer hosts.

    Idempotency & provenance are both handled with the labels system:
    every synced asset is tagged `netbox:<object_type>/<id>`. The tag is
    visible in the UI, queryable, and serves as the sync key, so no model
    change or migration is needed.
    """
    LABEL_NAME = 'netbox'

    def __init__(self):
        self.client = NetBoxClient()
        org_id = settings.NETBOX_SYNC_ORG_ID
        default_org = Organization.default()
        if org_id:
            self.org = Organization.get_instance(org_id, default=default_org)
        else:
            self.org = default_org
        self.asset_ct = ContentType.objects.get_for_model(Asset)
        self.stats = {
            'total': 0, 'created': 0, 'updated': 0, 'skipped': 0,
            'deactivated': 0, 'deleted': 0, 'errors': 0,
        }

    # ----- helpers -----

    @staticmethod
    def object_key(object_type, object_id):
        return '{}/{}'.format(object_type, object_id)

    @staticmethod
    def get_address(data):
        for field in ('primary_ip4', 'primary_ip6', 'primary_ip'):
            ip = data.get(field) or {}
            address = ip.get('address') or ''
            if address:
                return address.split('/')[0]
        return ''

    @staticmethod
    def get_platform(data):
        mapping = settings.NETBOX_PLATFORM_MAPPING or {}
        slug = (data.get('platform') or {}).get('slug') or ''
        names = []
        if slug and mapping.get(slug):
            names.append(mapping[slug])
        names.append(settings.NETBOX_DEFAULT_PLATFORM or 'Linux')
        for name in names:
            platform = Platform.objects.filter(name=name).first()
            if platform:
                return platform
        return None

    def get_node(self, data):
        values = [settings.NETBOX_SYNC_ROOT_NODE or 'NetBox']
        site = (data.get('site') or {}).get('name') or ''
        if site:
            values.append(site)
        return Node.create_nodes_recurse(values, Node.org_root())

    def get_label(self, object_type, object_id):
        label, __ = Label.objects.get_or_create(
            name=self.LABEL_NAME,
            value=self.object_key(object_type, object_id),
        )
        return label

    def find_asset(self, object_type, object_id):
        key = self.object_key(object_type, object_id)
        relation = LabeledResource.objects.filter(
            label__name=self.LABEL_NAME, label__value=key,
            res_type=self.asset_ct,
        ).first()
        if not relation:
            return None
        return Asset.objects.filter(id=relation.res_id).first()

    def get_unique_name(self, name, object_id):
        if not Asset.objects.filter(name=name).exists():
            return name
        return '{}-nb{}'.format(name, object_id)

    @staticmethod
    def set_protocols_from_platform(asset, platform):
        # e.g. for the builtin Linux platform ssh is primary while sftp is
        # default, the asset should get both
        protocols = platform.protocols.filter(
            Q(primary=True) | Q(required=True) | Q(default=True)
        )
        if not protocols:
            protocols = platform.protocols.all()
        for p in protocols:
            asset.protocols.get_or_create(name=p.name, defaults={'port': p.port})

    # ----- single object upsert / delete -----

    def sync_object(self, object_type, data):
        """
        Each object is wrapped in its own transaction: the asset signal
        handlers defer work with transaction.on_commit and (like API
        requests, which run inside ATOMIC_REQUESTS) expect an atomic
        block to be active when the multi-table Host insert happens.
        It also keeps asset + protocols + node + label consistent.
        """
        object_id = data.get('id')
        name = data.get('name') or 'netbox-{}'.format(object_id)
        address = self.get_address(data)
        if not address:
            logger.info('NetBox object %s/%s has no primary IP, skip', object_type, object_id)
            self.stats['skipped'] += 1
            return None

        with transaction.atomic():
            asset = self.find_asset(object_type, object_id)
            if asset:
                self.update_asset(asset, name, address, data)
                self.stats['updated'] += 1
                return asset

            platform = self.get_platform(data)
            if not platform:
                logger.error('NetBox sync: platform not found, check '
                             'NETBOX_DEFAULT_PLATFORM/NETBOX_PLATFORM_MAPPING')
                self.stats['errors'] += 1
                return None

            url = data.get('url') or ''
            asset = Host.objects.create(
                name=self.get_unique_name(name, object_id),
                address=address, platform=platform,
                comment='Synced from NetBox: {}'.format(url),
            )
            self.set_protocols_from_platform(asset, platform)
            node = self.get_node(data)
            if node:
                asset.nodes.add(node)
            label = self.get_label(object_type, object_id)
            LabeledResource.objects.get_or_create(
                label=label, res_type=self.asset_ct, res_id=str(asset.id),
            )
            self.stats['created'] += 1
            return asset

    def update_asset(self, asset, name, address, data):
        update_fields = []
        if asset.address != address:
            asset.address = address
            update_fields.append('address')
        if not asset.is_active:
            # the object is back in NetBox sync scope, re-enable it
            asset.is_active = True
            update_fields.append('is_active')
        if asset.name != name and not Asset.objects.filter(name=name).exclude(id=asset.id).exists():
            asset.name = name
            update_fields.append('name')
        if update_fields:
            asset.save(update_fields=update_fields)
        # Ensure membership of the computed node, but never remove the
        # asset from nodes that admins added manually.
        node = self.get_node(data)
        if node and not asset.nodes.filter(id=node.id).exists():
            asset.nodes.add(node)

    def remove_asset_by_key(self, key):
        action = settings.NETBOX_SYNC_DELETE_ACTION or DELETE_ACTION_DEACTIVATE
        if action == DELETE_ACTION_SKIP:
            return
        relations = LabeledResource.objects.filter(
            label__name=self.LABEL_NAME, label__value=key,
            res_type=self.asset_ct,
        )
        with transaction.atomic():
            for relation in relations:
                asset = Asset.objects.filter(id=relation.res_id).first()
                if not asset:
                    continue
                if action == DELETE_ACTION_DELETE:
                    asset.delete()
                    self.stats['deleted'] += 1
                elif asset.is_active:
                    asset.is_active = False
                    asset.save(update_fields=['is_active'])
                    self.stats['deactivated'] += 1

    # ----- entry points -----

    def get_enabled_object_types(self):
        object_types = []
        if settings.NETBOX_SYNC_DEVICES:
            object_types.append(DEVICE_OBJECT_TYPE)
        if settings.NETBOX_SYNC_VMS:
            object_types.append(VM_OBJECT_TYPE)
        return object_types

    def perform_sync(self):
        object_types = self.get_enabled_object_types()
        statuses = settings.NETBOX_SYNC_STATUSES or []

        with tmp_to_org(self.org):
            synced_keys = set()
            for object_type in object_types:
                for data in self.client.iter_objects_of_type(object_type, statuses):
                    self.stats['total'] += 1
                    key = self.object_key(object_type, data.get('id'))
                    try:
                        asset = self.sync_object(object_type, data)
                    except Exception as e:
                        logger.error('NetBox sync object %s failed: %s', key, e)
                        self.stats['errors'] += 1
                        continue
                    if asset is not None:
                        synced_keys.add(key)
            self.handle_stale_assets(object_types, synced_keys)

        logger.info('NetBox sync finished: %s', self.stats)
        return self.stats

    def handle_stale_assets(self, object_types, synced_keys):
        """
        Assets previously synced but absent from this run (deleted in
        NetBox, or moved out of the configured status scope). Only the
        object types synced this run are considered, so toggling device/vm
        sync off never mass-deactivates the other kind.
        """
        prefixes = tuple('{}/'.format(t) for t in object_types)
        known_keys = LabeledResource.objects.filter(
            label__name=self.LABEL_NAME, res_type=self.asset_ct,
        ).values_list('label__value', flat=True)
        for key in set(known_keys):
            if not key.startswith(prefixes):
                continue
            if key in synced_keys:
                continue
            self.remove_asset_by_key(key)

    def process_webhook_event(self, event, object_type, data):
        statuses = settings.NETBOX_SYNC_STATUSES or []
        status = (data.get('status') or {}).get('value') or ''
        key = self.object_key(object_type, data.get('id'))

        with tmp_to_org(self.org):
            if event == 'deleted':
                self.remove_asset_by_key(key)
            elif statuses and status and status not in statuses:
                # moved out of sync scope (e.g. active -> offline)
                self.remove_asset_by_key(key)
            else:
                self.sync_object(object_type, data)
        return self.stats
