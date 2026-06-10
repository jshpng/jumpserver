# -*- coding: utf-8 -*-
#
import time

from django.core.cache import cache
from django.db.models import Count, F

from assets.models import Asset
from audits.models import UserLoginLog
from common.utils import get_logger
from common.utils.timezone import local_zero_hour
from jumpserver.const import VERSION
from orgs.caches import OrgResourceStatisticsCache
from orgs.models import Organization
from orgs.utils import tmp_to_root_org
from terminal.models import Session
from users.models import User

logger = get_logger(__name__)

CORE_METRICS_CACHE_KEY = 'CORE_PROMETHEUS_METRICS_TEXT'
# Keep a short TTL: cheap enough to stay fresh, but protects the database
# from aggressive scrape intervals (multiple Prometheus, short intervals).
CORE_METRICS_CACHE_TTL = 20


class MetricFamily:
    """
    Minimal helper for the Prometheus text exposition format (0.0.4).

    Hand-rolled on purpose: it keeps the endpoint dependency-free (no
    prometheus_client requirement) and mirrors the style of the existing
    component metrics in terminal.utils.ComponentsPrometheusMetricsUtil.
    """

    def __init__(self, name, mtype='gauge', help_text=''):
        self.name = name
        self.mtype = mtype
        self.help_text = help_text
        self.samples = []

    @staticmethod
    def _escape_label_value(value):
        value = str(value)
        return value \
            .replace('\\', r'\\') \
            .replace('\n', r'\n') \
            .replace('"', r'\"')

    def add(self, value, **labels):
        self.samples.append((labels, value))
        return self

    def render(self):
        lines = []
        if self.help_text:
            lines.append('# HELP {} {}'.format(self.name, self.help_text))
        lines.append('# TYPE {} {}'.format(self.name, self.mtype))
        for labels, value in self.samples:
            if labels:
                label_text = ','.join([
                    '{}="{}"'.format(k, self._escape_label_value(v))
                    for k, v in sorted(labels.items())
                ])
                lines.append('{}{{{}}} {}'.format(self.name, label_text, value))
            else:
                lines.append('{} {}'.format(self.name, value))
        return lines


def get_db_status():
    t1 = time.time()
    try:
        ok = User.objects.first() is not None
        t2 = time.time()
        return ok, t2 - t1
    except Exception as e:
        return False, str(e)


def get_redis_status():
    key = 'HEALTH_CHECK'

    t1 = time.time()
    try:
        value = '1'
        cache.set(key, '1', 10)
        got = cache.get(key)
        t2 = time.time()

        if value == got:
            return True, t2 - t1
        return False, 'Value not match'
    except Exception as e:
        return False, str(e)


class CoreMetricsUtil:
    """
    Application level metrics about the JumpServer core service, designed
    to be scraped by Prometheus and visualized in Grafana.

    Counters that the web dashboard already maintains are read from
    OrgResourceStatisticsCache (root org scope), so scraping shares the
    cache with the console instead of issuing new heavy queries.
    """

    @staticmethod
    def get_info_metrics():
        family = MetricFamily(
            'jumpserver_info', 'gauge',
            'Build information of the JumpServer core service',
        )
        family.add(1, version=VERSION)
        return [family]

    @staticmethod
    def get_health_metrics():
        db_ok, db_time = get_db_status()
        redis_ok, redis_time = get_redis_status()

        status_family = MetricFamily(
            'jumpserver_health_status', 'gauge',
            'Health of core dependencies (1 healthy, 0 unhealthy)',
        )
        latency_family = MetricFamily(
            'jumpserver_health_latency_seconds', 'gauge',
            'Health check latency of core dependencies in seconds',
        )
        for component, ok, duration in [
            ('db', db_ok, db_time), ('redis', redis_ok, redis_time),
        ]:
            status_family.add(int(bool(ok)), component=component)
            if isinstance(duration, (int, float)):
                latency_family.add(round(duration, 6), component=component)
        return [status_family, latency_family]

    @staticmethod
    def get_resource_metrics():
        stats = OrgResourceStatisticsCache(Organization.root())
        gauges = [
            ('jumpserver_users_count', 'users_amount', 'Total users'),
            ('jumpserver_users_new_week_count', 'new_users_amount_this_week', 'Users created this week'),
            ('jumpserver_users_online_count', 'total_count_online_users', 'Online users'),
            ('jumpserver_assets_count', 'assets_amount', 'Total assets'),
            ('jumpserver_assets_new_week_count', 'new_assets_amount_this_week', 'Assets created this week'),
            ('jumpserver_assets_today_active_count', 'total_count_today_active_assets', 'Assets connected today'),
            ('jumpserver_nodes_count', 'nodes_amount', 'Total asset nodes'),
            ('jumpserver_zones_count', 'zones_amount', 'Total zones'),
            ('jumpserver_user_groups_count', 'groups_amount', 'Total user groups'),
            ('jumpserver_accounts_count', 'accounts_amount', 'Total accounts'),
            ('jumpserver_asset_permissions_count', 'asset_perms_amount', 'Total asset permissions'),
            ('jumpserver_sessions_online_count', 'total_count_online_sessions', 'Online sessions'),
            ('jumpserver_sessions_today_failed_count', 'total_count_today_failed_sessions', 'Failed sessions today'),
        ]

        families = []
        for name, attr, help_text in gauges:
            try:
                value = getattr(stats, attr)
            except Exception as e:
                logger.warning('Get metric %s failed: %s', name, e)
                continue
            if value is None:
                continue
            families.append(MetricFamily(name, 'gauge', help_text).add(value))
        return families

    @staticmethod
    def get_assets_by_type_metrics():
        family = MetricFamily(
            'jumpserver_assets_by_type_count', 'gauge',
            'Assets grouped by platform type',
        )
        with tmp_to_root_org():
            result = Asset.objects.annotate(tp=F('platform__type')) \
                .values('tp').order_by('tp').annotate(total=Count(1))
            for item in result:
                family.add(item['total'], type=item['tp'])
        return [family]

    @staticmethod
    def get_today_metrics():
        zero_hour = local_zero_hour()
        login_family = MetricFamily(
            'jumpserver_logins_today_count', 'gauge',
            'User login attempts since local midnight, grouped by status',
        )
        session_family = MetricFamily(
            'jumpserver_sessions_today_count', 'gauge',
            'Sessions started since local midnight',
        )
        with tmp_to_root_org():
            login_stats = UserLoginLog.objects.filter(datetime__gte=zero_hour) \
                .values('status').order_by('status').annotate(total=Count(1))
            counts = {bool(item['status']): item['total'] for item in login_stats}
            login_family.add(counts.get(True, 0), status='success')
            login_family.add(counts.get(False, 0), status='failed')

            session_count = Session.objects.filter(date_start__gte=zero_hour).count()
            session_family.add(session_count)
        return [login_family, session_family]

    collectors = (
        'get_info_metrics', 'get_health_metrics', 'get_resource_metrics',
        'get_assets_by_type_metrics', 'get_today_metrics',
    )

    def get_prometheus_metrics_text(self):
        text = cache.get(CORE_METRICS_CACHE_KEY)
        if text is not None:
            return text

        lines = []
        for collector in self.collectors:
            try:
                families = getattr(self, collector)()
            except Exception as e:
                logger.error('Collect core metrics %s failed: %s', collector, e)
                continue
            for family in families:
                lines.extend(family.render())
        text = '\n'.join(lines) + '\n'

        try:
            cache.set(CORE_METRICS_CACHE_KEY, text, CORE_METRICS_CACHE_TTL)
        except Exception as e:
            logger.warning('Cache core metrics text failed: %s', e)
        return text
