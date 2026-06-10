# JumpServer × Grafana 整合 (Observability)

本目录提供 JumpServer 与 Grafana 的完整监控整合:Prometheus 指标端点 + 开箱即用的
Grafana 仪表盘 + 一键启动的监控栈。

## 设计原则

1. **零新增依赖**:指标端点手写 Prometheus 文本格式(exposition format 0.0.4),
   不引入 `prometheus_client`,与既有的组件指标
   (`terminal.utils.ComponentsPrometheusMetricsUtil`)风格保持一致。
2. **复用已有缓存,不给数据库加压**:核心资源计数(用户/资产/在线会话等)直接读取
   Web 控制台同款的 `OrgResourceStatisticsCache`(root org 维度),指标抓取与控制台
   共享一份缓存;指标文本整体再加 20s Redis 缓存,多个 Prometheus 高频抓取也不会
   穿透到数据库。
3. **向后兼容**:
   - `/api/v1/prometheus/metrics/` 默认输出 = 新核心指标 + 原组件指标,
     已有的抓取配置无需变更;
   - 通过 `?scope=core` / `?scope=components` 可拆分抓取任务;
   - 原 `jumpserver_components_*` 指标名完全保留。
4. **盘活闲置配置**:`HEALTH_CHECK_TOKEN` 在配置中早已存在但从未生效,本次将其
   接到 metrics 端点上 —— 配置了 token 则要求
   `Authorization: Bearer <token>` 或 `?token=<token>`(`hmac.compare_digest`
   常数时间比较);默认空 token 时行为与从前一致(开放),不破坏存量部署。
   `/api/health/` 保持开放,避免影响 K8s/LB 探针。
5. **多组织全局视角**:跨组织的统计查询统一包在 `tmp_to_root_org()` 中执行,
   保证指标是全局口径而非某个组织的局部口径。

## 暴露的指标

| 指标 | 类型 | 说明 |
|------|------|------|
| `jumpserver_info{version}` | gauge | 核心服务版本信息 |
| `jumpserver_health_status{component="db\|redis"}` | gauge | 依赖健康度 (1/0) |
| `jumpserver_health_latency_seconds{component}` | gauge | 依赖健康检查延迟 |
| `jumpserver_users_count` | gauge | 用户总数 |
| `jumpserver_users_online_count` | gauge | 在线用户数 |
| `jumpserver_users_new_week_count` | gauge | 本周新增用户 |
| `jumpserver_assets_count` | gauge | 资产总数 |
| `jumpserver_assets_by_type_count{type}` | gauge | 按平台类型分布的资产数 |
| `jumpserver_assets_new_week_count` | gauge | 本周新增资产 |
| `jumpserver_assets_today_active_count` | gauge | 今日活跃资产 |
| `jumpserver_nodes_count` / `jumpserver_zones_count` | gauge | 节点 / 网域数量 |
| `jumpserver_user_groups_count` | gauge | 用户组数量 |
| `jumpserver_accounts_count` | gauge | 账号总数 |
| `jumpserver_asset_permissions_count` | gauge | 资产授权规则数量 |
| `jumpserver_sessions_online_count` | gauge | 在线会话数 |
| `jumpserver_sessions_today_count` | gauge | 今日会话数 |
| `jumpserver_sessions_today_failed_count` | gauge | 今日失败会话数 |
| `jumpserver_logins_today_count{status="success\|failed"}` | gauge | 今日登录(按结果) |
| `jumpserver_components_*` | gauge | 原有组件指标(koko/lion/celery 等) |

> 命名说明:以上均为瞬时值(gauge),按照 Prometheus 惯例不使用 `_total`
> 后缀;既有 `jumpserver_components_*_total` 指标为保持兼容原样保留。

## 快速开始

```bash
cd utils/grafana
# 1. 修改 prometheus/prometheus.yml 中的 target 指向你的 core 服务
# 2. (可选) 在 JumpServer config.yml 设置 HEALTH_CHECK_TOKEN 并在
#    prometheus.yml 中配置同样的 Bearer token
docker compose up -d
# Grafana: http://localhost:3000 (admin / jumpserver)
# 仪表盘自动出现在 "JumpServer" 目录:Overview / Components
```

手工验证端点:

```bash
curl 'http://<core>:8080/api/v1/prometheus/metrics/?scope=core'
# 配置了 HEALTH_CHECK_TOKEN 时:
curl -H 'Authorization: Bearer <token>' 'http://<core>:8080/api/v1/prometheus/metrics/'
```

## 告警建议

可以在 Prometheus / Grafana Alerting 中基于以下表达式配置告警:

```promql
# 依赖故障
jumpserver_health_status == 0
# 组件离线
sum by (component_type) (jumpserver_components_status_total{status="offline"}) > 0
# 登录失败突增(15 分钟内增长超过 50 次)
delta(jumpserver_logins_today_count{status="failed"}[15m]) > 50
# 组件内存 / 磁盘水位
jumpserver_components_memory_used > 90
jumpserver_components_disk_used > 90
```

## 反向整合:用 JumpServer 纳管 Grafana

除了"Grafana 监控 JumpServer",也可以反向把 Grafana 本身作为资产纳入
JumpServer 审计:

1. 控制台 → 资产管理 → 资产列表 → 创建,类型选择 **Web → Website**;
2. 地址填 Grafana 登录页 URL(如 `https://grafana.example.com/login`),
   按页面元素配置用户名/密码选择器(Grafana 默认登录表单:
   username `input[name=user]`、password `input[name=password]`、
   提交按钮 `button[type=submit]`);
3. 为其绑定 Grafana 账号后,即可通过 Web 会话代填登录并全程录像审计。

这样两个方向闭环:Grafana 看 JumpServer 的运行状态,JumpServer 管 Grafana 的
访问与审计。

## 目录结构

```
utils/grafana/
├── docker-compose.yml              # Prometheus + Grafana 一键启动
├── prometheus/prometheus.yml       # 抓取配置示例(含 token 写法)
└── grafana/
    ├── provisioning/
    │   ├── datasources/prometheus.yml   # 数据源自动注册
    │   └── dashboards/provider.yml      # 仪表盘自动加载
    └── dashboards/
        ├── jumpserver-overview.json     # 核心概览仪表盘
        └── jumpserver-components.json   # 组件监控仪表盘
```
