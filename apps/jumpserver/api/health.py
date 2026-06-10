import hmac
import time

from django.conf import settings
from django.http.response import HttpResponse, JsonResponse
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from terminal.utils import ComponentsPrometheusMetricsUtil
from .metrics import CoreMetricsUtil, get_db_status, get_redis_status


class HealthApiMixin(APIView):
    pass


class HealthCheckView(HealthApiMixin):
    permission_classes = (AllowAny,)

    @staticmethod
    def get_db_status():
        return get_db_status()

    @staticmethod
    def get_redis_status():
        return get_redis_status()

    def get(self, request):
        redis_status, redis_time = self.get_redis_status()
        db_status, db_time = self.get_db_status()
        status = all([redis_status, db_status])
        data = {
            'status': status,
            'db_status': db_status,
            'db_time': db_time,
            'redis_status': redis_status,
            'redis_time': redis_time,
            'time': int(time.time()),
        }
        return Response(data)


class PrometheusMetricsApi(HealthApiMixin):
    """
    Prometheus exposition endpoint, designed to be scraped by Prometheus
    and visualized with the Grafana dashboards shipped in utils/grafana/.

    Scopes (?scope=):
      - components: terminal component metrics (historical behavior)
      - core: application level metrics of the core service
      - all (default): both

    If HEALTH_CHECK_TOKEN is configured, requests must carry it either as
    `?token=<token>` or `Authorization: Bearer <token>`. When the token is
    empty (default) the endpoint stays open, keeping backward compatibility.
    """
    permission_classes = (AllowAny,)

    @staticmethod
    def get_request_token(request):
        token = request.query_params.get('token')
        if token:
            return token
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        prefix = 'Bearer '
        if auth_header.startswith(prefix):
            return auth_header[len(prefix):]
        return ''

    def is_token_valid(self, request):
        required_token = getattr(settings, 'HEALTH_CHECK_TOKEN', '') or ''
        if not required_token:
            return True
        token = self.get_request_token(request) or ''
        return hmac.compare_digest(required_token, token)

    @staticmethod
    def get_components_metrics_text():
        util = ComponentsPrometheusMetricsUtil()
        return util.get_prometheus_metrics_text()

    @staticmethod
    def get_core_metrics_text():
        util = CoreMetricsUtil()
        return util.get_prometheus_metrics_text()

    def get(self, request, *args, **kwargs):
        if not self.is_token_valid(request):
            return JsonResponse(status=401, data={'error': 'Invalid metrics token'})

        scope = request.query_params.get('scope', 'all')
        if scope == 'components':
            metrics_texts = [self.get_components_metrics_text()]
        elif scope == 'core':
            metrics_texts = [self.get_core_metrics_text()]
        else:
            metrics_texts = [
                self.get_core_metrics_text(),
                self.get_components_metrics_text(),
            ]
        metrics_text = '\n'.join(metrics_texts)
        return HttpResponse(metrics_text, content_type='text/plain; version=0.0.4; charset=utf-8')
