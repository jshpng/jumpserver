# JumpServer × NetBox 整合 (CMDB 资产同步)

把 NetBox 作为资产的事实来源(Source of Truth),JumpServer 自动从 NetBox 同步
设备(`dcim.device`)与虚拟机(`virtualization.virtualmachine`)为可登录、可审计的
主机资产。支持两种互补的同步通道:

1. **拉取(Pull)**:全量同步任务,可手动触发,也可按 interval/crontab 周期执行;
2. **推送(Push / Webhook)**:NetBox 对象创建/修改/删除时实时推送,秒级生效。

两种方式建议同时启用:Webhook 负责实时性,周期全量负责最终一致性(兜底对账)。

## 设计原则与巧思

1. **标签即溯源、即幂等键**:每个同步来的资产都会打上
   `netbox:<object_type>/<id>` 标签(如 `netbox:dcim.device/42`)。
   - 在 UI 上一眼可见资产来自 NetBox;
   - 同步引擎用它做幂等匹配(改名/换 IP 都不会产生重复资产);
   - 不需要给 Asset 模型加字段,**零数据库迁移**。
2. **安全的删除策略**:NetBox 侧删除(或移出状态范围)的对象,默认只**禁用**资产
   (`deactivate`),不删数据;可配置为 `skip`(不动)或 `delete`(真删)。
   对账时只考虑本轮同步的对象类型 —— 关掉"同步虚拟机"开关不会误禁用已同步的
   虚拟机资产。
3. **Webhook 安全模型**:
   - 必须同时打开 `NETBOX_SYNC_ENABLED` 与 `NETBOX_WEBHOOK_ENABLED`,否则 404;
   - 必须配置 `NETBOX_WEBHOOK_SECRET`,并通过 HMAC-SHA512 校验
     `X-Hook-Signature`(`hmac.compare_digest` 常数时间比较)——
     **没有密钥就无法启用**,不存在"裸奔"的中间态;
   - 校验通过后立即转交 Celery 异步处理并返回 202,NetBox 不会因为同步慢而超时重试。
4. **最小侵入更新**:更新已有资产时只改 address/激活状态/名称,并确保资产挂在
   计算出的节点上,但**绝不移除**管理员手工添加的其他节点;`comment` 中写入
   NetBox 对象 URL 方便回跳,且只在创建时写入,不覆盖人工备注。
5. **复用平台协议模板**:新建资产的协议直接复制所选平台的默认协议
   (default → primary → 全部),开箱即可连接,无需手工补 SSH 端口。
6. **架构与 LDAP 同步对齐**:配置项 + serializer `post_save` 动态注册周期任务 +
   `settings/utils` 同步引擎 + `settings/tasks` Celery 任务 + testing/sync API,
   完全沿用 LDAP 用户同步的成熟模式,降低维护成本。

## 资产映射规则

| NetBox | JumpServer |
|--------|------------|
| `dcim.device` / `virtualization.virtualmachine` | Host 资产 |
| `primary_ip4` → `primary_ip6` | 资产地址(剥离 CIDR 掩码;无主 IP 则跳过并计数) |
| `name` | 资产名称(重名时自动加 `-nb<id>` 后缀) |
| `site.name` | 节点:`/<根节点>/<站点>`(默认根节点 `NetBox`) |
| `platform.slug` | 经 `NETBOX_PLATFORM_MAPPING` 映射到 JumpServer 平台,默认 `Linux` |
| `status` | 仅同步 `NETBOX_SYNC_STATUSES` 中的状态(默认 `["active"]`) |
| 对象 URL | 资产 comment(仅创建时写入) |

## 配置项

可写在 `config.yml` / 环境变量,或通过
`PUT /api/v1/settings/setting/?category=netbox` 动态配置(无需重启):

| 配置 | 默认 | 说明 |
|------|------|------|
| `NETBOX_SYNC_ENABLED` | `false` | 总开关 |
| `NETBOX_BASE_URL` | `''` | 如 `https://netbox.example.com` |
| `NETBOX_API_TOKEN` | `''` | NetBox API Token(只读权限即可,加密存储) |
| `NETBOX_VERIFY_SSL` | `true` | 校验 NetBox 证书 |
| `NETBOX_SYNC_DEVICES` | `true` | 同步设备 |
| `NETBOX_SYNC_VMS` | `true` | 同步虚拟机 |
| `NETBOX_SYNC_STATUSES` | `["active"]` | 状态过滤,空列表为全部 |
| `NETBOX_SYNC_ROOT_NODE` | `NetBox` | 资产树根节点名 |
| `NETBOX_PLATFORM_MAPPING` | `{}` | `{netbox platform slug: JumpServer 平台名}` |
| `NETBOX_DEFAULT_PLATFORM` | `Linux` | 映射不到时的兜底平台 |
| `NETBOX_SYNC_DELETE_ACTION` | `deactivate` | `skip` / `deactivate` / `delete` |
| `NETBOX_SYNC_ORG_ID` | `''` | 资产归属组织,空为默认组织 |
| `NETBOX_SYNC_IS_PERIODIC` | `false` | 周期同步开关 |
| `NETBOX_SYNC_INTERVAL` | `24` | 周期(小时) |
| `NETBOX_SYNC_CRONTAB` | `''` | Crontab 表达式(优先于 interval) |
| `NETBOX_WEBHOOK_ENABLED` | `false` | Webhook 开关 |
| `NETBOX_WEBHOOK_SECRET` | `''` | Webhook 共享密钥(加密存储) |

## API

```bash
# 1. 写入配置(动态生效,密文字段加密落库)
curl -X PUT 'https://<jms>/api/v1/settings/setting/?category=netbox' \
  -H 'Authorization: Bearer <jms_token>' -H 'Content-Type: application/json' \
  -d '{
    "NETBOX_SYNC_ENABLED": true,
    "NETBOX_BASE_URL": "https://netbox.example.com",
    "NETBOX_API_TOKEN": "<netbox_token>",
    "NETBOX_SYNC_IS_PERIODIC": true,
    "NETBOX_SYNC_CRONTAB": "30 2 * * *"
  }'

# 2. 连通性测试(返回 NetBox 版本)
curl -X POST 'https://<jms>/api/v1/settings/netbox/testing/' \
  -H 'Authorization: Bearer <jms_token>' -H 'Content-Type: application/json' -d '{}'

# 3. 手动触发全量同步(返回 Celery 任务 ID,可在任务中心查看)
curl -X POST 'https://<jms>/api/v1/settings/netbox/sync/' \
  -H 'Authorization: Bearer <jms_token>'
```

## 在 NetBox 侧配置 Webhook

NetBox → Operations → Integrations → Webhooks → Add:

| 字段 | 值 |
|------|-----|
| URL | `https://<jms>/api/v1/settings/netbox/webhook/` |
| HTTP method | `POST` |
| HTTP content type | `application/json` |
| Secret | 与 `NETBOX_WEBHOOK_SECRET` 一致 |
| SSL verification | 按需 |

再到 Event Rules(NetBox 3.x 为 Webhook 页内选项)绑定对象类型
`DCIM > Device`、`Virtualization > Virtual Machine`,事件勾选
Creations / Updates / Deletions。

> NetBox 用该 Secret 对请求体做 HMAC-SHA512 签名并放进
> `X-Hook-Signature` 头,JumpServer 侧用同样算法校验。

## 同步语义速查

| 场景 | 行为 |
|------|------|
| NetBox 新增 active 设备/VM(有主 IP) | 创建 Host,打标签、挂节点、配协议 |
| 无主 IP | 跳过(计入 skipped) |
| 改 IP / 改名 | 更新地址;无重名冲突时跟随改名 |
| 站点变更 | 加入新站点节点,保留人工挂载的节点 |
| 状态移出范围(如 active → offline) | 按删除策略处理(默认禁用) |
| NetBox 删除对象 | 按删除策略处理(默认禁用) |
| 对象重新回到范围内 | 自动重新激活同一资产(标签幂等) |

## 已知边界

- 同步目标为单一组织(`NETBOX_SYNC_ORG_ID`);多组织映射(如按 NetBox tenant
  分组织)可在后续版本基于本引擎扩展。
- 资产凭据(账号/密码)不在同步范围 —— 凭据生命周期属于 JumpServer 账号管理
  / 改密计划的职责。
- Web UI(lina)暂无 NetBox 设置页,需通过 API 或 `config.yml` 配置;
  serializer 已就绪,前端表单可直接对接 `category=netbox`。
