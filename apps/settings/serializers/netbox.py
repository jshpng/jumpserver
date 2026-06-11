# -*- coding: utf-8 -*-
#
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from common.serializers.fields import EncryptedField

__all__ = ['NetBoxSettingSerializer']


class NetBoxSettingSerializer(serializers.Serializer):
    PREFIX_TITLE = _('NetBox')

    NETBOX_SYNC_ENABLED = serializers.BooleanField(
        required=False, default=False, label=_('NetBox'),
        help_text=_('Enable asset synchronization from NetBox CMDB')
    )
    NETBOX_BASE_URL = serializers.CharField(
        max_length=1024, required=False, allow_blank=True, label=_('NetBox URL'),
        help_text=_('The base URL of the NetBox service. For example: https://netbox.example.com')
    )
    NETBOX_API_TOKEN = EncryptedField(
        max_length=256, required=False, allow_blank=True, label=_('API token'),
        help_text=_('NetBox REST API token, read permission is sufficient')
    )
    NETBOX_VERIFY_SSL = serializers.BooleanField(
        required=False, default=True, label=_('Verify SSL'),
    )
    NETBOX_SYNC_DEVICES = serializers.BooleanField(
        required=False, default=True, label=_('Sync devices'),
        help_text=_('Synchronize NetBox devices (dcim.device)')
    )
    NETBOX_SYNC_VMS = serializers.BooleanField(
        required=False, default=True, label=_('Sync virtual machines'),
        help_text=_('Synchronize NetBox virtual machines (virtualization.virtualmachine)')
    )
    NETBOX_SYNC_STATUSES = serializers.ListField(
        child=serializers.CharField(max_length=32), required=False,
        default=['active'], label=_('Status filter'),
        help_text=_('Only objects in these NetBox statuses are synchronized, '
                    'an empty list means all statuses')
    )
    NETBOX_SYNC_ROOT_NODE = serializers.CharField(
        max_length=128, required=False, default='NetBox', label=_('Root node'),
        help_text=_('Synced assets are placed under this node, '
                    'grouped by their NetBox site')
    )
    NETBOX_PLATFORM_MAPPING = serializers.JSONField(
        required=False, default=dict, label=_('Platform mapping'),
        help_text=_('Mapping from NetBox platform slug to JumpServer platform name, '
                    'e.g. {"ubuntu-22": "Linux", "windows-2022": "Windows"}')
    )
    NETBOX_DEFAULT_PLATFORM = serializers.CharField(
        max_length=128, required=False, default='Linux', label=_('Default platform'),
        help_text=_('Fallback JumpServer platform when no mapping matches')
    )
    NETBOX_SYNC_DELETE_ACTION = serializers.ChoiceField(
        choices=[
            ('skip', _('Do nothing')),
            ('deactivate', _('Deactivate')),
            ('delete', _('Delete')),
        ],
        required=False, default='deactivate', label=_('Removal action'),
        help_text=_('Action applied to assets whose NetBox object was deleted '
                    'or moved out of the status scope')
    )
    NETBOX_SYNC_ORG_ID = serializers.CharField(
        max_length=36, required=False, allow_blank=True, default='',
        label=_('Organization'),
        help_text=_('Organization ID that synced assets belong to, '
                    'empty means the default organization')
    )
    NETBOX_SYNC_IS_PERIODIC = serializers.BooleanField(
        required=False, default=False, label=_('Periodic run'),
    )
    NETBOX_SYNC_INTERVAL = serializers.IntegerField(
        required=False, allow_null=True, default=24,
        max_value=65535, min_value=1, label=_('Interval'),
        help_text=_('Unit: hour')
    )
    NETBOX_SYNC_CRONTAB = serializers.CharField(
        required=False, max_length=128, allow_null=True, allow_blank=True,
        label=_('Crontab'),
    )
    NETBOX_WEBHOOK_ENABLED = serializers.BooleanField(
        required=False, default=False, label=_('Webhook'),
        help_text=_('Accept NetBox webhooks for near real-time synchronization')
    )
    NETBOX_WEBHOOK_SECRET = EncryptedField(
        max_length=256, required=False, allow_blank=True, label=_('Webhook secret'),
        help_text=_('Shared secret used to verify the X-Hook-Signature header, '
                    'required when webhook is enabled')
    )

    def validate(self, attrs):
        is_periodic = attrs.get('NETBOX_SYNC_IS_PERIODIC')
        crontab = attrs.get('NETBOX_SYNC_CRONTAB')
        interval = attrs.get('NETBOX_SYNC_INTERVAL')
        if is_periodic and not any([crontab, interval]):
            raise serializers.ValidationError(
                _("Require interval or crontab setting"))

        webhook_enabled = attrs.get('NETBOX_WEBHOOK_ENABLED')
        webhook_secret = attrs.get('NETBOX_WEBHOOK_SECRET') \
                         or getattr(settings, 'NETBOX_WEBHOOK_SECRET', '')
        if webhook_enabled and not webhook_secret:
            raise serializers.ValidationError(
                _("Webhook secret is required when webhook is enabled"))
        return super().validate(attrs)

    def post_save(self):
        # Deferred import to avoid a circular dependency:
        # settings.tasks -> settings.utils -> ... -> settings.serializers
        # (same pattern as the LDAP setting serializer)
        from ..tasks.netbox import import_netbox_assets_periodic
        keys = [
            'NETBOX_SYNC_IS_PERIODIC', 'NETBOX_SYNC_INTERVAL', 'NETBOX_SYNC_CRONTAB',
        ]
        kwargs = {k: self.validated_data[k] for k in keys if k in self.validated_data}
        if not kwargs:
            return
        import_netbox_assets_periodic(**kwargs)
