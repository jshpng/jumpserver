# -*- coding: utf-8 -*-
#
import hashlib
import hmac

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView, Response

from common.utils import get_logger
from settings.models import Setting
from .. import serializers
from ..tasks.netbox import import_netbox_assets, process_netbox_webhook_event
from ..utils.netbox import NetBoxClient, WEBHOOK_MODEL_OBJECT_TYPES

logger = get_logger(__file__)

__all__ = ['NetBoxTestingAPI', 'NetBoxSyncAPI', 'NetBoxWebhookAPI']


class NetBoxTestingAPI(GenericAPIView):
    serializer_class = serializers.NetBoxSettingSerializer
    rbac_perms = {
        'POST': 'settings.change_other'
    }

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        base_url = data.get('NETBOX_BASE_URL') or settings.NETBOX_BASE_URL
        token = data.get('NETBOX_API_TOKEN') or settings.NETBOX_API_TOKEN
        verify_ssl = data.get('NETBOX_VERIFY_SSL', settings.NETBOX_VERIFY_SSL)

        if not base_url:
            return Response(
                status=status.HTTP_400_BAD_REQUEST,
                data={'error': _('NetBox URL is required')}
            )

        client = NetBoxClient(base_url=base_url, token=token, verify_ssl=verify_ssl)
        try:
            data = client.get_status()
        except Exception as e:
            return Response(status=status.HTTP_400_BAD_REQUEST, data={'error': str(e)})

        version = data.get('netbox-version', '')
        msg = _('Test success')
        if version:
            msg = '{} (NetBox {})'.format(msg, version)
        return Response(status=status.HTTP_200_OK, data={'msg': msg})


class NetBoxSyncAPI(APIView):
    perm_model = Setting
    rbac_perms = {
        'POST': 'settings.change_other'
    }

    def post(self, request, *args, **kwargs):
        if not settings.NETBOX_SYNC_ENABLED:
            return Response(
                status=status.HTTP_400_BAD_REQUEST,
                data={'error': _('NetBox sync is not enabled')}
            )
        task = import_netbox_assets.delay()
        return Response({'task': task.id}, status=status.HTTP_201_CREATED)


class NetBoxWebhookAPI(APIView):
    """
    Receiver for NetBox webhooks (near real-time sync).

    Security model: the endpoint is anonymous but only works when both
    NETBOX_SYNC_ENABLED and NETBOX_WEBHOOK_ENABLED are on, and it always
    requires a valid HMAC-SHA512 `X-Hook-Signature` computed with the
    shared NETBOX_WEBHOOK_SECRET (constant-time comparison). Without a
    configured secret the endpoint refuses to process anything, so it can
    never be enabled in an unauthenticated state. Processing is delegated
    to Celery so NetBox gets a fast 202 response.
    """
    authentication_classes = []
    permission_classes = (AllowAny,)

    @staticmethod
    def verify_signature(request):
        secret = settings.NETBOX_WEBHOOK_SECRET or ''
        if not secret:
            return False
        signature = request.META.get('HTTP_X_HOOK_SIGNATURE', '')
        if not signature:
            return False
        digest = hmac.new(
            secret.encode('utf-8'), request.body, digestmod=hashlib.sha512
        ).hexdigest()
        return hmac.compare_digest(digest, signature)

    def post(self, request, *args, **kwargs):
        if not settings.NETBOX_SYNC_ENABLED or not settings.NETBOX_WEBHOOK_ENABLED:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if not self.verify_signature(request):
            return Response(
                status=status.HTTP_401_UNAUTHORIZED,
                data={'error': 'Invalid webhook signature'}
            )

        payload = request.data or {}
        event = payload.get('event')
        model = payload.get('model')
        data = payload.get('data')

        if event not in ('created', 'updated', 'deleted') or not isinstance(data, dict):
            return Response(
                status=status.HTTP_400_BAD_REQUEST,
                data={'error': 'Unsupported webhook payload'}
            )
        if model not in WEBHOOK_MODEL_OBJECT_TYPES:
            # Not a model we sync, acknowledge and ignore
            return Response(status=status.HTTP_204_NO_CONTENT)

        process_netbox_webhook_event.delay(event, model, data)
        return Response(status=status.HTTP_202_ACCEPTED, data={'msg': 'accepted'})
