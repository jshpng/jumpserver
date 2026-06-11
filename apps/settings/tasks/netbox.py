# coding: utf-8
#
from celery import shared_task
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from common.utils import get_logger
from ops.celery.decorator import after_app_ready_start
from .ldap import register_periodic_task
from ..utils.netbox import NetBoxImporter, WEBHOOK_MODEL_OBJECT_TYPES

__all__ = [
    'import_netbox_assets', 'import_netbox_assets_periodic',
    'process_netbox_webhook_event',
]

logger = get_logger(__file__)


@shared_task(
    verbose_name=_('Import assets from NetBox'),
    description=_(
        "Synchronize devices and virtual machines from the NetBox CMDB "
        "into the asset tree"
    )
)
def import_netbox_assets():
    if not settings.NETBOX_SYNC_ENABLED:
        logger.info('NetBox sync is disabled, skip')
        return
    importer = NetBoxImporter()
    return importer.perform_sync()


@shared_task(
    verbose_name=_('Process NetBox webhook event'),
    description=_(
        "When a NetBox webhook is received, this task is invoked to apply "
        "the change (create/update/delete) to the matching asset"
    )
)
def process_netbox_webhook_event(event, model, data):
    if not settings.NETBOX_SYNC_ENABLED or not settings.NETBOX_WEBHOOK_ENABLED:
        logger.info('NetBox webhook is disabled, skip')
        return
    object_type = WEBHOOK_MODEL_OBJECT_TYPES.get(model)
    if not object_type:
        logger.info('NetBox webhook model %s is not supported, skip', model)
        return
    importer = NetBoxImporter()
    return importer.process_webhook_event(event, object_type, data)


@shared_task(
    verbose_name=_('Registration periodic import NetBox assets task'),
    description=_(
        """When NetBox auto-sync parameters change, such as Crontab parameters,
        the NetBox sync task will be re-registered or updated, and this task
        will be invoked"""
    )
)
@after_app_ready_start
def import_netbox_assets_periodic(**kwargs):
    register_periodic_task(
        'import_netbox_assets_periodic', import_netbox_assets,
        'NETBOX_SYNC_INTERVAL', 'NETBOX_SYNC_IS_PERIODIC',
        'NETBOX_SYNC_CRONTAB', **kwargs
    )
