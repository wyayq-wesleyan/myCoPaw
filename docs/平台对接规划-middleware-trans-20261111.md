# 汇智小助手 × middleware_trans 对接规划

> 状态：规划稿，未开始实施
> 日期：2026-11-11
> 适用：汇智小助手 ARM64 / AMD64 内网部署
> 关联项目：`/Users/wesley/Desktop/projects/middleware_trans`

---

## 0. 结论摘要

1. **middleware_trans 目前不具备生产接入条件。** 它已经不是“能不能调通”的问题，而是
   SSO Token 协议、TCMS 流程发起、TCMS 审批回调、网关鉴权四条主链路都还没有闭环，
   且没有在真实 MSP/TCMS/ELC 环境做过端到端验证。汇智小助手不能把生产登录和审批
   建立在这个状态上。
2. **汇智小助手侧应先做“身份/权限/审批/审计”四层抽象，再谈接平台。** 业务代码不直接
   调用 middleware，只依赖内部 provider 接口。这样 `local` 和 `platform` 两种模式
   可以共用同一套业务逻辑，也避免以后被 middleware 的 DTO 变化绑死。
3. **推荐生产使用 `platform` 模式，开发/离线/演示使用 `local` 模式。** 两种模式在
   安装时由配置选定，写入模式标记后**不允许运行中切换**；确需变更时走离线迁移工具，
   不靠改一个环境变量。
4. **“平台权限管理”不能理解成把每个模型、技能、工具都搬到 MSP。** 平台管身份、组织、
   角色和粗粒度能力包；汇智小助手用“能力包 → 资源画像”的映射解析到具体菜单、工具、
   技能、模型。这是唯一可维护的边界，否则 MSP 里会出现几百个随版本变化的权限码。
5. **审批流是最大工程量，不是加一个 HTTP 调用。** TCMS 审批可能持续几分钟到几小时，
   现有汇智审批是进程内 Future，重启即丢；必须改成持久化审批 + 可恢复的工具调用，
   否则平台审批通过后原任务已经不存在了。
6. **在 middleware 通过“接入就绪门禁”之前，汇智只做抽象层、Mock 和契约测试，不做
   真实平台联调。** 否则双方会在不断变化的字段和语义上反复返工。

---

## 1. middleware_trans 现状

### 1.1 它是什么

`middleware_trans` 是 Spring Boot 2.7 / Java 8 的**协议转换网关**，把公司内部 Java 体系的
Ice RPC 能力封装成 HTTP/JSON，给 Python/Go/Node 等非 Java 系统调用：

```text
非 Java 系统 ──HTTP/JSON──▶ middleware_trans (:9089) ──Ice RPC──▶ MSP / TCMS / ELC / SSO
```

它在 MSP/TCMS/ELC/SSO 中是**独立注册的系统**，需要自己的：

- `systemCode`（MSP 重发/系统标识）
- SSO `systemId`
- TCMS 回调 `serviceId` / `method`
- ELC `clientSystemCode`

不能沿用 qhdHD 或 ELC 现有业务系统的编码。

### 1.2 已有能力

| 模块 | 能力 | 现状 |
| --- | --- | --- |
| SSO | Token 校验、用户/组织/角色查询、登录 URL | 登录 URL 可用；**Token 校验协议不对** |
| MSP | 用户、组织、角色、权限、菜单、系统、字典、消息、待办 | 代码路径完整，**未真实联调**；无网关鉴权 |
| TCMS | 案件/档案/目标查询、流程发起、状态、回调 | 查询待联调；**流程发起/回调/状态更新不可用** |
| ELC | 单条/登录/操作/批量日志 | 客户端已实现，**未验证 ELC 实际接收** |
| 回调 | archiveCode → HTTP callback 骨架 | **未注册、未监听、内存存储** |
| 部署 | Dockerfile / Compose / 配置模板 | 文件就绪，完整镜像待构建 |

### 1.3 与汇智对接直接相关的缺口

按严重程度排序：

1. **SSO Token 校验协议不匹配**
   - 正式 SSO 使用 `ClientHttpCommand.sendCommand(tokenUrl)`，从 **HTTP 响应头**取
     `username` / `tssotoken`，请求需带 `sId`、`systemId`、`rurl`；
   - 当前 `SsoService` 用 `HttpURLConnection` 读 body，忽略 `sessionId`/`rurl`；
   - 影响：真实 SSO 环境下 `/sso/validate` 可能永远失败。
2. **TCMS 流程发起不可用**
   - `FlowStartReq` 缺少 `caseCode`、`caseId`、查询时间范围、查询对象等 TCMS 必需字段；
   - `TcmsService` 里 `new TcmsFlowManager()`，Spring Bean 的 `@Value`/DAO 都没注入；
   - `FlowStartResp` 丢掉 `archiveCode`、`instId`、`approvalPageURL`、`exempt`；
   - 影响：即使调通，汇智也拿不到审批页地址和回调关联键。
3. **TCMS 审批回调没有送达**
   - `IceServerConfig` 没有创建 Ice Communicator，`MiddlewareCallbackServant` 没注册；
   - `CallbackStore` 是单机内存，重启丢失、多实例不共享；
   - 没有重试、幂等、签名；
   - 影响：审批完成后汇智收不到结果，工具调用永远挂起。
4. **网关无鉴权**
   - 除 `/sso/validate` 外，50 多个 MSP/TCMS/ELC 接口没有 API Key、mTLS 或 token 校验；
   - CORS `allowedOriginPattern("*")` + credentials；
   - 影响：内网任意机器都能查用户、组织、案件、菜单，甚至写 ELC 日志。
5. **ELC 字段与异步语义不完整**
   - `ElcLogReq` 是简化模型，缺 `logDataType`、`opUserName`、`opDesc`、附件等；
   - 客户端异步发送，HTTP 200 ≠ ELC 已接收；
   - `/usr/elcLog` 需要持久化。
6. **测试与构建不可重复**
   - 约 146 个 `@Test`，但未在 CI 执行，部分测试源码编译不过；
   - 没有真实 MSP/TCMS/ELC 集成测试；
   - 公网无法构建（依赖内网 Nexus）。

### 1.4 对接含义

汇智不能把 middleware 当成“已经稳定、只差调用”的服务。正确的做法是：

- 先把 middleware 的**契约和就绪门禁**定义清楚；
- 汇智侧先完成 provider 抽象和 Mock，联调时只替换 adapter；
- 双方共同维护一份 OpenAPI/JSON Schema，字段变更走版本化。

---

## 2. 对接边界与原则

### 2.1 汇智使用 middleware 的四类能力

| 能力 | middleware 接口 | 汇智用途 |
| --- | --- | --- |
| 登录 | `/api/v1/sso/login-url`、`/api/v1/sso/validate` | 平台单点登录，换取用户身份 |
| 权限 | `/api/v1/msp/user/{userCode}/rights`、`/menus`、组织/角色查询 | 解析平台角色、权限码、菜单 |
| 审批 | `/api/v1/tcms/flow/start`、状态查询、回调 | 高风险工具/数据操作审批 |
| 执法日志 | `/api/v1/elc/log`、`/logs/batch` | 登录、操作、导出、审批等审计 |

### 2.2 明确留在汇智本地的能力

以下能力不应塞进 MSP 权限模型：

- 每个用户的文件、会话、知识空间、数据表、智能体隔离；
- 模型、技能、工具的具体授权（用“能力包 → 资源画像”解析）；
- 智能体运行时、会话历史、定时任务、文件解析、知识检索；
- 汇智自己的管理员页面（平台模式下按平台权限开放）；
- 沙箱、文件布局、输出目录等产品约束。

### 2.3 设计原则

1. **业务代码不直接调 middleware**：只依赖 `IdentityProvider` / `AuthorizationProvider` /
   `ApprovalProvider` / `AuditSink` 四个接口。
2. **统一内部身份模型**：平台用户和本地用户都归一为 `UserIdentity`，业务层不区分来源。
3. **模式在安装时锁定**：`HUIZHI_AUTH_MODE=local|platform`，写入模式标记；运行中改配置
   直接拒绝启动。
4. **默认拒绝**：平台权限码未映射到能力时，默认无权限，不做“猜测”。
5. **安全链路失败关闭**：登录、权限、审批依赖不可用时，受保护操作拒绝，不自动放行。
6. **审计链路失败缓冲**：ELC 不可用时写本地 outbox，不阻塞用户操作，但要告警和补投。
7. **契约优先**：所有字段、错误码、状态语义以双方确认的契约为准，不靠“看代码猜”。

---

## 3. 需要现在拍板的关键决策

| 编号 | 决策 | 推荐 | 说明 |
| --- | --- | --- | --- |
| D1 | 生产认证模式 | `platform` | 平台统一登录；`local` 仅用于开发/离线/演示 |
| D2 | 权限粒度 | 平台“能力包”+ 汇智“资源画像” | 不把每个工具/模型搬到 MSP |
| D3 | 管理员判定 | 平台权限 `HUIZHI_ADMIN` | 不靠角色名称；可选 break-glass 本地账号 |
| D4 | 审批范围 | 高风险操作走 TCMS；低风险本地策略 | 必须明确清单，不能“全部审批” |
| D5 | 审批持久化 | 持久化审批 + 可恢复工具调用 | 不能依赖进程内 Future |
| D6 | 多组织 | v1 一组织一实例 | 避免全量数据模型加 `org_code` |
| D7 | 身份主键 | 平台不可变 `userId` | 不能用 `loginName` 做长期主键 |
| D8 | 会话生命周期 | 本地短 token + 平台会话绑定 | 平台登出/过期后本地 token 失效 |
| D9 | 模式变更 | 默认禁止；仅离线迁移工具 | 满足“确定后不支持变更” |
| D10 | 网关安全 | mTLS 或 API Key + IP 白名单 | middleware 目前无鉴权，必须先补 |

> 建议把 D1–D10 写成一页 ADR（架构决策记录），由项目、平台、运维三方签字。
> 后续代码、测试、上线检查都以这页为准。

---

## 4. 目标架构

```text
┌──────────────────────────────────────────────────────────────┐
│ 汇智小助手                                                    │
│                                                              │
│  Console / API ──▶ AuthService ──▶ IdentityProvider ────────┐│
│       │                 │                                   ││
│       │                 ├──▶ AuthorizationProvider ────────┐││
│       │                 │                                   │││
│       │                 ├──▶ ApprovalProvider ─────────────┐│││
│       │                 │                                   ││││
│       └──────────────▶ AuditSink ──────────────────────────┐││││
│                                                             │││││
└─────────────────────────────────────────────────────────────┘││││
                                                              ││││
        ┌─────────────────────────────────────────────────────┘│││
        │  local adapter                    platform adapter   │││
        ▼                                        ▼             │││
  auth.json / state.json              middleware_trans :9089    ││
  local approval service               ├─ SSO / MSP            ││
  local audit (file/DB)                ├─ TCMS                 ││
                                       └─ ELC                  ││
```

### 4.1 内部模块建议

```text
src/qwenpaw/integrations/platform/
  config.py          # 配置解析、启动校验、模式标记
  client.py          # HTTP 客户端：超时、重试、熔断、mTLS/API Key、脱敏日志
  models.py          # 平台 DTO 与内部模型的转换
  identity.py        # PlatformIdentityProvider
  authorization.py   # 平台权限 → CapabilitySnapshot
  approval.py        # TCMS 审批 + 回调校验 + 状态机
  audit.py           # ELC 审计 + outbox worker
  errors.py          # 统一错误类型与用户可读提示
src/qwenpaw/auth/providers/
  base.py            # IdentityProvider / AuthorizationProvider 协议
  local.py           # 现有 auth.json 逻辑
  platform.py        # 平台模式编排
src/qwenpaw/app/approvals/
  durable.py         # 持久化审批存储（PG）
  resume.py          # 审批通过后恢复工具调用
```

### 4.2 内部统一模型

```python
@dataclass(frozen=True)
class UserIdentity:
    internal_id: str          # 稳定内部 ID，由 external_id 派生，不随登录名变化
    login_name: str
    display_name: str
    org_code: str | None
    org_path: tuple[str, ...]
    roles: tuple[Role, ...]
    source: Literal["local", "platform"]
    external_id: str | None   # 平台不可变用户 ID
    enabled: bool
    attributes: Mapping[str, str]

@dataclass(frozen=True)
class AuthorizationSnapshot:
    user_id: str
    source: str
    capabilities: frozenset[str]   # menu.* / tool.* / skill.* / model.* / data.* / admin.*
    resource_profile: str          # 例如 standard / analyst / admin
    platform_rights: frozenset[str]
    version: str                   # 哈希，用于缓存和审计
    fetched_at: datetime
    expires_at: datetime

@dataclass
class ApprovalTicket:
    approval_id: str              # 汇智内部 UUID，同时作为 businessId
    provider: Literal["local", "tcms"]
    external_flow_id: str | None
    archive_code: str | None
    approval_url: str | None
    status: ApprovalStatus
    expires_at: datetime
    payload_hash: str
```

### 4.3 新增数据表（PostgreSQL `platform` schema）

```text
platform.users
  internal_id, login_name, external_id, display_name, org_code, source,
  enabled, created_at, updated_at
  unique(source, external_id), unique(source, login_name)

platform.user_sessions
  session_id, user_id, auth_source, external_session_id,
  issued_at, expires_at, revoked_at, client_ip, user_agent

platform.authorization_snapshots
  user_id, version, capabilities_json, platform_rights_json,
  fetched_at, expires_at

platform.approval_requests
  approval_id, user_id, session_id, agent_id, tool_name, action_class,
  request_payload_json, request_hash, provider, status,
  external_flow_id, archive_code, approval_url,
  created_at, updated_at, expires_at, attempts, last_error

platform.approval_callbacks
  id, approval_id, received_at, source_ip, signature_valid,
  payload_json, processed, error

platform.audit_outbox
  id, event_id, event_type, payload_json, created_at,
  attempts, next_attempt_at, delivered_at, last_error
```

> 现有 `auth.json` 在 `platform` 模式下仍可作为 break-glass 凭据文件，但不再是用户
> 主数据源。`local` 模式下保持现状。

---

## 5. 配置设计

### 5.1 模式与平台连接

```bash
# 认证模式：local | platform。安装时选定，不允许运行中切换。
HUIZHI_AUTH_MODE=platform
HUIZHI_AUTH_MODE_LOCK=strict

# middleware 连接
HUIZHI_PLATFORM_BASE_URL=https://middleware.internal:9089
HUIZHI_PLATFORM_API_KEY=...                 # 或使用 mTLS，二选一
HUIZHI_PLATFORM_MTLS_CERT=/run/secrets/huizhi-client.crt
HUIZHI_PLATFORM_MTLS_KEY=/run/secrets/huizhi-client.key
HUIZHI_PLATFORM_CA=/run/secrets/internal-ca.crt
HUIZHI_PLATFORM_TIMEOUT_SECONDS=5
HUIZHI_PLATFORM_RETRIES=2
HUIZHI_PLATFORM_SYSTEM_CODE=huizhi-assistant

# SSO
HUIZHI_PLATFORM_SSO_ENABLED=true
HUIZHI_PLATFORM_SSO_LOGIN_URL=              # 为空时由 middleware /sso/login-url 返回
HUIZHI_PLATFORM_SSO_CALLBACK_URL=https://huizhi.internal/api/auth/sso/callback
HUIZHI_PLATFORM_SSO_STATE_SECRET=...
HUIZHI_PLATFORM_SSO_SESSION_TTL_SECONDS=28800

# 权限
HUIZHI_PLATFORM_MENU_SYSTEM_CODE=HUIZHI
HUIZHI_PLATFORM_ADMIN_RIGHTS=HUIZHI_ADMIN
HUIZHI_PLATFORM_PERMISSION_TTL_SECONDS=300
HUIZHI_PLATFORM_PERMISSION_STALE_GRACE_SECONDS=3600

# 审批
HUIZHI_PLATFORM_APPROVAL_ENABLED=true
HUIZHI_PLATFORM_APPROVAL_FLOW_TYPE=HUIZHI_TOOL_APPROVAL
HUIZHI_PLATFORM_APPROVAL_CALLBACK_URL=https://huizhi.internal/api/integrations/tcms/callback
HUIZHI_PLATFORM_APPROVAL_CALLBACK_SECRET=...
HUIZHI_PLATFORM_APPROVAL_TIMEOUT_SECONDS=86400

# 审计
HUIZHI_PLATFORM_ELC_ENABLED=true
HUIZHI_PLATFORM_ELC_SYSTEM_CODE=huizhi-assistant
HUIZHI_PLATFORM_ELC_BATCH_SIZE=50
HUIZHI_PLATFORM_ELC_FLUSH_INTERVAL_SECONDS=5

# 应急
HUIZHI_BREAK_GLASS_ENABLED=false
```

> 所有密钥只放 `SECRET_DIR`/容器 secret，不写进 `.env` 明文、不进代码库、不进日志。

### 5.2 能力映射文件

`config/platform/capability-map.yaml`：

```yaml
version: 1
platform:
  menu_system_code: HUIZHI
  admin_rights: [HUIZHI_ADMIN]
  admin_roles: [HUIZHI_ADMIN]

# 平台权限码 -> 汇智能力
rights:
  HUIZHI_BASIC:
    - menu.chat
    - menu.sessions
    - menu.files
    - menu.inbox
  HUIZHI_KNOWLEDGE:
    - menu.knowledge
    - tool.knowledge_search
    - tool.get_document_detail
  HUIZHI_DATA_ANALYST:
    - menu.data-analysis
    - tool.create_data_table
    - tool.import_data_table
    - tool.run_sql_query
  HUIZHI_CRON:
    - menu.cron-jobs
    - tool.create_cron_job
  HUIZHI_ADMIN:
    - "*"

# 能力包 -> 资源画像
profiles:
  basic:
    models: [standard]
    skills: [core]
    tools: [file_read, file_write, knowledge_search]
  analyst:
    models: [standard, reasoning]
    skills: [core, data-analysis]
    tools: [file_read, file_write, knowledge_search, create_data_table, import_data_table, run_sql_query]
  admin:
    models: ["*"]
    skills: ["*"]
    tools: ["*"]
```

> 映射文件变更需要版本号和审计；不能让运维随手改完不记录。

### 5.3 审批策略文件

`config/platform/approval-policy.yaml`：

```yaml
version: 1
rules:
  - id: sensitive-data-query
    match:
      capabilities: [tool.run_sql_query, tool.export_data]
    provider: tcms
    flow_type: HUIZHI_DATA_QUERY
    severity: high
  - id: destructive-shell
    match:
      tools: [execute_shell_command]
      risk: [high, critical]
    provider: tcms
    flow_type: HUIZHI_TOOL_APPROVAL
    severity: high
  - id: skill-install
    match:
      capabilities: [tool.install_skill, tool.install_plugin]
    provider: tcms
    flow_type: HUIZHI_CONFIG_CHANGE
    severity: medium
defaults:
  provider: local       # platform 模式下也必须显式列出；不要隐式回退
```

> 关键：**哪些操作必须走平台审批，要在上线前写死并评审。** 不能运行时按心情改。

---

## 6. 登录与会话

### 6.1 推荐流程（授权码 / 一次性票据）

不要直接把 SSO token 放在浏览器 URL 或 localStorage 里长期保存。

```text
1. 浏览器打开汇智 /login
2. 汇智生成 state + nonce，写入服务端短期缓存
3. 汇智返回/跳转 middleware /sso/login-url（或配置的 SSO 登录页）
   targetUrl = https://huizhi.internal/api/auth/sso/callback
4. 用户在 SSO 登录
5. SSO 回调汇智 callback，携带 tssotoken / sessionId / state
6. 汇智校验 state，调用 middleware /sso/validate
7. middleware 调 SSO/MSP，返回 loginName、realName、org、roles、userId
8. 汇智 upsert platform.users，拉取 rights，生成 AuthorizationSnapshot
9. 汇智签发自己的短 token（HttpOnly Cookie 或 Bearer），跳转 /chat
```

### 6.2 Token 设计

- 继续使用现有 HMAC 签名 token，但 claims 增加：
  - `sub`：内部 `internal_id`
  - `login_name`
  - `auth_source: platform`
  - `external_id`
  - `org_code`
  - `session_id`
  - `cap_version`
  - `exp`（不超过平台会话剩余时间）
- token 有效期建议 8 小时，且不得长于 SSO 会话；支持滑动续期，但每次续期需确认平台
  会话仍有效。
- 平台登出、禁用、会话过期时，汇智 token 必须失效：
  - 主动：logout 调 middleware/SSO 登出；
  - 被动：短 TTL + 定时校验 `/sso/validate` 或平台会话 introspection；
  - 权限变更：`cap_version` 变化后强制刷新。

### 6.3 失败与降级

| 场景 | 行为 |
| --- | --- |
| middleware/SSO 不可用，新登录 | 503 + 明确提示；不绕过登录 |
| 已登录用户，middleware 短暂不可用 | 现有 token 继续用；权限用 stale grace；超过 grace 后受保护操作拒绝 |
| 平台会话过期 | 下一次请求 401，前端跳 SSO 重新登录 |
| break-glass 开启 | 仅本地 admin 账号 + 强密码 + IP 白名单 + 审计告警；默认关闭 |
| 平台用户被禁用 | 下次权限刷新/校验失败，立即拒绝，不等待 token 过期 |

---

## 7. 权限模型

### 7.1 能力分类

汇智内部统一使用 capability 字符串，不让业务代码理解平台权限码：

```text
menu.chat / menu.files / menu.knowledge / menu.data-analysis / menu.sessions
menu.cron-jobs / menu.skills / menu.tools / menu.inbox / menu.user-management
tool.<tool_name>
skill.<skill_name>
model.<provider>/<model>
data.scope.self | data.scope.org | data.scope.all
admin.users | admin.settings | admin.models | admin.audit
```

### 7.2 解析顺序

```text
平台 rights（MSP）
  → capability-map.yaml 的 rights 映射
  → 得到 capabilities
  → 按最高能力包确定 resource_profile
  → 合并 profile 的 models/skills/tools 默认集合
  → 与本地“平台模式禁用项”取交集
  → 生成 AuthorizationSnapshot
```

规则：

- 未映射的 rights：忽略并记录 warning，不报错、不猜测；
- `HUIZHI_ADMIN`：映射为 `*`，但仍受“平台模式禁用项”和沙箱约束；
- 默认拒绝：没有 `menu.*` 就不显示菜单，没有 `tool.*` 就不注册工具；
- 权限快照带版本哈希，token 里只放版本，不放大对象；
- 权限刷新失败：在 `PERMISSION_STALE_GRACE` 内用旧快照，超过后受保护操作拒绝，
  但已打开页面可继续只读浏览（按产品决定）。

### 7.3 管理员语义

- 平台模式：`HUIZHI_ADMIN` 是唯一管理员来源；汇智本地 `admin` 账号默认禁用。
- 平台模式下，用户管理页只读或隐藏；用户/角色/组织变更在 MSP 完成。
- 如果平台没有细粒度管理员，可配置 `admin_roles`，但必须写进 ADR。
- break-glass：单独开关、单独审计、单独告警；不能成为常规运维入口。

### 7.4 为什么不建议“纯平台细粒度权限”

MSP 的权限码通常对应 Java 系统的菜单/按钮。汇智有：

- 10 个左右菜单；
- 几十个工具；
- 几十个技能；
- 多个模型；
- 每个版本都可能新增工具/技能。

如果每个资源都在 MSP 建权限码，平台管理员会被版本发布拖死，且权限码与代码版本
强耦合。正确做法是平台管“能力包”，汇智管“能力包里的资源画像”。

---

## 8. 审批流

### 8.1 审批动作分类

| 类别 | 例子 | 建议 provider |
| --- | --- | --- |
| A. 敏感数据查询/导出 | 查询案件数据、导出 CSV/Excel、跨库查询 | TCMS |
| B. 高风险工具 | shell、删文件、批量改数据、外部 API 写 | TCMS |
| C. 外部数据源访问 | 第三方 PG/MySQL/Oracle/Hive | TCMS 或本地策略 |
| D. 配置变更 | 安装技能/插件、改模型、改权限 | TCMS |
| E. 管理员操作 | 建用户、改权限、看审计 | 本地管理员 + ELC 记录 |

> 不是所有工具调用都要审批。全量审批会让用户关掉审批或绕过系统，反而降低安全性。
> 建议按“能力 + 风险等级”配置，且清单在上线前评审。

### 8.2 TCMS 请求契约（需要 middleware 补齐）

当前 `FlowStartReq` 只有：

```json
{
  "loginName": "zhangsan",
  "taskId": "task_123",
  "flowType": "DATA_QUERY",
  "reason": "...",
  "systemCode": "middleware",
  "callbackUrl": "https://..."
}
```

建议扩展为：

```json
{
  "loginName": "zhangsan",
  "userId": "平台用户ID",
  "businessId": "汇智审批UUID",
  "flowType": "HUIZHI_TOOL_APPROVAL",
  "reason": "用户在会话中请求导出案件数据",
  "systemCode": "huizhi-assistant",
  "callbackUrl": "https://huizhi.internal/api/integrations/tcms/callback",
  "caseCode": "可选，案件编号",
  "caseId": "可选，案件ID",
  "caseName": "可选",
  "targets": ["可选，查询对象"],
  "timeRange": {"start": "...", "end": "..."},
  "context": {
    "sessionId": "...",
    "agentId": "...",
    "toolName": "...",
    "riskLevel": "high",
    "summary": "人类可读的审批摘要"
  }
}
```

响应必须包含：

```json
{
  "flowId": "...",
  "businessId": "...",
  "archiveCode": "...",
  "instId": "...",
  "status": "PENDING",
  "approvalPageURL": "https://tcms.../approve?instId=...",
  "exempt": false
}
```

### 8.3 审批状态机

```text
NOT_REQUIRED
    │
    ▼
PENDING ──approve──▶ APPROVED ──▶ 恢复工具执行
    │
    ├──reject──▶ REJECTED
    ├──timeout──▶ EXPIRED
    ├──cancel──▶ CANCELLED
    └──error──▶ FAILED（可重试/人工处理）
```

要求：

- 状态存 PG，不存内存；
- `businessId` 全局唯一，作为幂等键；
- 回调重复到达只处理一次；
- 回调乱序（先收到 APPROVED 再收到 PENDING）按状态机拒绝降级；
- 超时后不允许“自动通过”，只能拒绝或重新发起；
- 每次状态变更写审计事件（ELC + 本地）。

### 8.4 持久化审批与工具恢复

现有汇智审批是：

```text
工具调用 → ApprovalService.create_pending → Future 等待 → 用户点同意 → 继续执行
```

问题：

- 进程重启，Future 丢失；
- 平台审批可能几小时，不能一直占着协程/连接；
- 多实例部署时，审批请求可能落到另一个实例。

建议改为：

```text
1. 工具调用命中审批策略
2. 生成 approval_id，序列化工具调用上下文（工具名、参数、agent、session、checkpoint）
3. 写 platform.approval_requests，状态 PENDING
4. 调 TCMS 发起流程，保存 flowId/archiveCode/approvalURL
5. 向聊天返回“待审批”卡片 + 审批链接
6. 审批回调/轮询确认后，状态 APPROVED
7. 后台 worker 读取待恢复记录，在对应 session/agent 中重新调度工具调用
8. 工具执行完成，更新审批记录并写审计
```

关键点：

- 工具参数可能含敏感数据，持久化时要加密/脱敏；
- 恢复时要重新校验权限（用户可能已被禁用/权限已回收）；
- 如果原会话已删除，审批结果只记录，不执行，并告警；
- 同一 `approval_id` 只能恢复一次（幂等）；
- 恢复失败要可重试，不能静默丢弃。

### 8.5 回调接口

汇智暴露：

```text
POST /api/integrations/tcms/callback
```

要求：

- mTLS 或 HMAC 签名（`X-Signature`、`X-Timestamp`、`X-Nonce`）；
- 校验时间窗口（±5 分钟）和 nonce 防重放；
- 只接受 middleware 来源 IP；
- 请求体落库后再返回 200，处理异步化；
- 返回体明确 `{"code":0}`，避免 middleware 重试；
- 处理失败返回 5xx，让 middleware 重试；
- 记录原始 payload 和签名校验结果，便于追责。

### 8.6 审批 UX

- 聊天中出现“待审批”卡片，显示：工具、风险、摘要、审批链接、过期时间；
- 用户点击后在**新标签页**打开 TCMS 审批页，不阻塞聊天；
- 审批完成后，聊天卡片自动更新（SSE/轮询）；
- 审批被拒/超时，卡片显示原因，工具不执行；
- 管理员可在“审批中心”查看待办/历史（可选）；
- 定时任务/后台任务触发审批时，必须通知到人（站内信/待办），不能静默挂起。

### 8.7 案件上下文

TCMS 的很多流程围绕案件。汇智现有“路径/案件”（`p_<uuid>`）是内部数据隔离概念，
不等于 TCMS 的 `caseCode`/`caseId`。需要：

- 建立 `platform.case_bindings`：汇智 path_id ↔ TCMS caseCode/caseId；
- 在创建路径/智能体时选择关联案件；
- 没有关联案件的操作，使用“无案件”流程类型（需要 TCMS 支持）或只走本地审批；
- 审批摘要里带案件编号，便于审批人判断。

> 如果 TCMS 只支持案件类流程，而汇智大量操作与案件无关，那么“所有审批走 TCMS”
> 在产品上不成立。这是必须在 Phase 0 确认的问题。

---

## 9. ELC 执法日志（审计）

### 9.1 事件范围（建议）

必须记录：

- 登录成功/失败、登出、会话过期；
- 工具调用开始/结束/失败（含工具名、风险等级、耗时）；
- 文件上传/下载/导出/删除；
- 知识空间查询、文档预览/下载；
- 数据表查询、导入、导出、SQL 执行；
- 审批发起/通过/拒绝/超时；
- 用户/角色/权限/模型配置变更；
- 平台对接失败（登录校验失败、权限拉取失败、回调验签失败）。

不记录：

- 完整 prompt、完整文件内容、模型输出全文；
- 密码、token、API Key、连接串；
- 可还原的敏感数据（按脱敏规则处理）。

### 9.2 ELC 字段映射

middleware `ElcLogReq` 当前字段：

```text
module, operationType, operationDesc, loginName, clientIp,
requestUrl, requestMethod, requestParams, result, costTime
```

汇智映射建议：

| 汇智字段 | ELC 字段 | 说明 |
| --- | --- | --- |
| `event_type` | `module` | 查询类/分析类/导出类/查看类/管理类 |
| `action` | `operationType` | 例如 `TOOL_EXECUTE`、`FILE_DOWNLOAD` |
| `summary` | `operationDesc` | 人类可读，脱敏 |
| `login_name` | `loginName` | 平台登录名 |
| `client_ip` | `clientIp` | 取真实客户端 IP，信任代理要配置 |
| `request_path` | `requestUrl` | 不含 query 中的 token |
| `method` | `requestMethod` | |
| `params_summary` | `requestParams` | 脱敏/截断/哈希 |
| `result` | `result` | SUCCESS/FAILURE |
| `duration_ms` | `costTime` | |

如果 middleware 支持扩展字段，建议增加：

```text
requestId, sessionId, agentId, approvalId, caseCode, riskLevel, outcomeCode
```

### 9.3 Outbox 设计

```text
业务事件 → 本地 audit_outbox（PG）
         → 后台 worker 批量 POST /api/v1/elc/logs/batch
         → 成功标记 delivered_at
         → 失败指数退避，超过阈值进 dead-letter + 告警
```

要求：

- 至少一次投递，`event_id` 作为幂等键；
- 单条失败不影响整批；
- 批量大小/间隔可配置；
- 监控积压量和最老事件年龄；
- ELC 不可用时用户操作不阻塞，但审计告警必须可见；
- 容器重启不丢（PG 持久化）。

### 9.4 异步语义

middleware 的 ELC 客户端是异步的，HTTP 200 只代表“进入本地队列”。因此：

- 汇智侧不能把“ELC 接口返回成功”当成“审计已完成”；
- 需要 ELC 侧提供查询/对账接口，或至少提供日志接收确认；
- 关键操作（审批、导出）建议双写：ELC + 本地审计文件/表，便于自查。

---

## 10. 数据隔离与多组织

### 10.1 不变的原则

- 普通用户只能看自己的文件、会话、知识、数据表、智能体；
- 平台角色不会自动带来跨用户数据权限；
- 管理员跨用户可见必须由 `HUIZHI_ADMIN` 显式授予；
- 沙箱、文件布局、输出目录约束继续生效，不因平台接入放宽。

### 10.2 组织范围

如果平台要求“同组织可见”，需要新增 `data.scope.org` 能力，并在所有查询/文件路径
加 `org_code` 过滤。这是一次全量数据模型改造，工作量大。

建议：

- v1：**一组织一实例**，不做组织内共享；
- v2：确有需求再引入 `org_code`，并做数据迁移；
- 不要为了“以后可能要用”提前把 `org_code` 加到所有表，收益低、风险高。

### 10.3 多租户

如果 middleware 后面接多个公安单位，建议一个单位一套汇智实例（独立 PG/存储），
而不是一套实例多租户。理由：

- 数据隔离和合规要求高；
- 现有代码按单实例单组织设计，改造成本大；
- 故障域、升级窗口、审计边界更清晰。

---

## 11. 兼容与迁移

### 11.1 模式锁定

启动时：

```text
读取 HUIZHI_AUTH_MODE
读取 SECRET_DIR/auth-mode.json 和 platform.meta
if 标记不存在:
    写入标记（mode, installed_at, installation_id, config_hash）
elif 标记.mode != 环境变量:
    拒绝启动，错误信息给出迁移命令
```

标记内容示例：

```json
{
  "mode": "platform",
  "installed_at": "2026-11-11T10:00:00+08:00",
  "installation_id": "...",
  "config_hash": "...",
  "contract_version": "middleware-v1"
}
```

### 11.2 local → platform 迁移

步骤：

1. 备份 `auth.json`、`data/`、PG；
2. 导出本地用户清单（loginName、role、menu_permissions、资源权限）；
3. 平台侧确认每个用户都有对应账号；
4. 建立映射：`local loginName` → `platform userId`；
5. 预检：缺账号、重名、禁用、组织缺失；
6. 迁移：写 `platform.users`，保留 `_slug`，使原有文件/数据仍归属本人；
7. 本地密码：禁用或改为 break-glass；
8. 平台模式下重新拉取权限，生成快照；
9. 冒烟：登录、文件、知识、数据、审批、审计；
10. 回滚：恢复备份 + 模式标记回 `local`（仅限迁移窗口内）。

### 11.3 platform → local

默认不支持。只有平台长期不可用、且经过审批的灾难恢复场景才允许，按“降级运行”
处理，并明确：

- 本地账号密码重置；
- 平台期间产生的数据归属；
- 恢复平台后如何合并；
- 审计如何补写。

### 11.4 身份映射

- 优先使用平台**不可变 userId**；
- `loginName` 只用于显示和初次匹配；
- 平台改名/调岗时，靠 `external_id` 保持数据归属；
- 如果 middleware 短期拿不到 userId，必须在契约里补，不能长期用 loginName 当主键。

---

## 12. 失败模式与降级

| 依赖 | 失败场景 | 汇智行为 | 是否阻塞用户 |
| --- | --- | --- | --- |
| SSO/middleware | 新登录不可用 | 503 + 维护提示；不绕过 | 是（新登录） |
| middleware | 权限刷新失败 | 旧快照 grace 内继续；超时后受保护操作拒绝 | 部分 |
| middleware | 用户查询失败 | 已登录会话继续；管理页只读 | 否 |
| TCMS | 审批发起失败 | 高风险操作拒绝，提示稍后重试；不自动放行 | 是（该操作） |
| TCMS | 回调丢失 | 定时轮询状态；超时转人工 | 是（该操作） |
| ELC | 写入失败 | outbox 缓冲、重试、告警 | 否 |
| 本地 PG | 不可用 | 汇智整体不可用（现有行为） | 是 |
| 时钟 | 偏差 | 回调/签名用服务器时间 + 容差；监控 NTP | 可能 |

关键原则：

- **安全链路失败关闭，审计链路失败缓冲。**
- 任何“平台不可用就自动通过审批/自动给管理员权限”的设计都不可接受。

---

## 13. 安全要求

### 13.1 middleware 侧必须先补

- 网关鉴权：mTLS 或 API Key（至少一个），按调用方发放，可轮换；
- 来源 IP 白名单；
- CORS 收紧到汇智域名，不允许 `*` + credentials；
- 日志脱敏：不打印 `tssotoken`、密码、完整请求体；
- 限流：按调用方/接口；
- 健康检查区分 liveness/readiness；
- 回调签名、重试、幂等、持久化；
- 错误码规范：不要所有失败都返回 HTTP 200 + `code=-1`，至少区分鉴权失败/参数错误/
  下游不可用/业务拒绝。

### 13.2 汇智侧

- token 存 HttpOnly + Secure + SameSite Cookie，避免 localStorage XSS 窃取；
  若保留 Bearer，需要接受 XSS 风险并做 CSP；
- SSO state/nonce 防 CSRF；回调 URL 白名单，防开放重定向；
- 回调 HMAC + 时间戳 + nonce + IP 白名单；
- 平台客户端证书/API Key 放 secret，定期轮换；
- 不在日志、审计、错误信息中输出 token/密码/连接串；
- 平台返回的用户信息、权限码做长度/字符校验，防注入/日志伪造；
- 管理接口继续做服务端权限校验，不能只靠前端隐藏菜单；
- 定期做越权测试（IDOR、跨用户文件、跨会话审批）。

### 13.3 合规

- 审批记录、审计日志的保留期限、不可篡改性、访问权限要明确；
- ELC 日志可能包含个人信息，需脱敏和最小化；
- 平台 token/用户信息属于敏感数据，跨系统传输要加密；
- 数据出境/出单位边界要符合内网规定。

---

## 14. 可观测性

### 14.1 指标

```text
huizhi_auth_login_total{result, mode}
huizhi_auth_login_duration_seconds
huizhi_platform_request_total{endpoint, status}
huizhi_platform_request_duration_seconds
huizhi_permission_refresh_total{result}
huizhi_permission_snapshot_age_seconds
huizhi_approval_total{provider, status}
huizhi_approval_pending_count
huizhi_approval_callback_total{result}
huizhi_approval_resume_total{result}
huizhi_audit_outbox_size
huizhi_audit_outbox_oldest_age_seconds
huizhi_audit_delivery_total{result}
```

### 14.2 日志

统一字段：

```text
request_id, user_id, login_name, session_id, agent_id,
approval_id, case_code, event_type, result, duration_ms
```

禁止记录：

```text
password, token, api_key, secret, 完整文件内容, 完整 prompt
```

### 14.3 健康检查

- `/api/healthz`：进程存活，不依赖外部；
- `/api/readyz`：数据库、middleware、TCMS/ELC 连通性；
- 平台模式启动时打印：模式、平台地址、systemCode、契约版本、依赖状态。

---

## 15. 分阶段实施计划

> 工期是相对估计，前提是 middleware 团队同步投入；如果 middleware 不修，Phase 2 以后
> 无法开始。

### Phase 0：契约与门禁（1–2 周）

- 输出 ADR：D1–D10 决策、模式锁定、权限边界、审批范围；
- 与平台团队确认 SSO/TCMS/ELC 契约、字段、错误码、状态语义；
- 输出 middleware 就绪门禁清单（见第 17 节）；
- 建测试环境：middleware + 测试 MSP/TCMS/ELC 账号；
- 确定能力包/资源画像/审批策略清单。

**交付物**：ADR、OpenAPI/JSON Schema、门禁清单、测试计划。

### Phase 1：汇智内部抽象（1–2 周）

- 引入 `IdentityProvider` / `AuthorizationProvider` / `ApprovalProvider` / `AuditSink`；
- 把现有 `auth.json` 逻辑迁到 `LocalIdentityProvider`，行为不变；
- 引入 `AuthorizationSnapshot` 和能力模型；
- 加模式配置和模式标记；
- 全部现有测试通过，新增 provider 单测。

**交付物**：抽象层、local 适配器、模式锁、回归报告。

### Phase 2：平台登录与权限（2–3 周）

- `PlatformIdentityProvider`：登录 URL、回调、token 校验、用户映射；
- `PlatformAuthorizationProvider`：rights/menus 拉取、能力映射、缓存/刷新；
- 前端登录页按模式切换；
- Mock middleware 契约测试；
- 实验室真实 middleware 联调（前提：Phase 0 门禁通过）。

**交付物**：平台登录、权限映射、联调报告、安全测试报告。

### Phase 3：ELC 审计（1–2 周）

- 审计事件模型、outbox、worker、重试/告警；
- 登录/工具/文件/知识/数据/审批事件接入；
- ELC 字段映射和脱敏；
- 与 ELC 侧对账。

**交付物**：审计链路、outbox 监控、ELC 验收记录。

### Phase 4：TCMS 审批（3–4 周）

- 持久化审批表、状态机；
- TCMS 发起/查询/回调；
- 回调签名/幂等/重放防护；
- 审批通过后工具恢复（checkpoint/重新调度）；
- 审批 UX（卡片、链接、状态更新）；
- 案件绑定（如需要）。

**交付物**：审批全链路、故障演练、恢复测试。

### Phase 5：试点与切换（2 周）

- 小范围试点（1 个组织/5–10 用户）；
- 观察指标、问题修复；
- 正式切换，保留 local 回滚路径；
- 运维手册、应急演练。

**交付物**：试点报告、切换方案、回滚演练记录、运维手册。

---

## 16. 测试与验收

### 16.1 测试层次

| 层次 | 内容 | 门禁 |
| --- | --- | --- |
| 单元 | provider 选择、能力映射、token claims、模式锁、状态机 | 必须全绿 |
| 契约 | 用 OpenAPI/Mock 校验请求/响应/错误码 | 字段不匹配即失败 |
| 集成 | 真实 middleware + 测试 MSP/TCMS/ELC | 关键链路全通 |
| 安全 | 越权、重放、伪造回调、开放重定向、SSRF、注入 | 无高危 |
| 故障 | 超时、500、断网、重复/乱序回调、ELC 挂 | 行为符合降级表 |
| 性能 | 登录 p95、权限刷新、审批发起、回调延迟 | 满足 SLO |
| 迁移 | 100+ 用户 local→platform、回滚 | 数据归属正确 |
| 回归 | 现有全量单元/契约/P0/前端 | 无新增失败 |

### 16.2 关键验收用例

1. 平台用户首次登录，自动建本地映射，能打开自己的文件/会话；
2. 平台用户 A 不能看 B 的文件、会话、知识、数据表；
3. 平台管理员能看全部，普通用户看不到管理菜单；
4. 平台权限回收后，最长 `PERMISSION_TTL + grace` 内生效；
5. 平台用户被禁用后，下一次请求拒绝；
6. 平台登出后，汇智 token 失效；
7. 高风险工具触发 TCMS 审批，审批页可打开；
8. 审批通过后，工具在原会话恢复执行；
9. 审批拒绝/超时，工具不执行，用户看到原因；
10. 回调重复/乱序不重复执行；
11. 回调伪造/过期签名被拒绝；
12. 审批期间汇智重启，审批记录不丢，恢复后能继续；
13. ELC 挂掉时用户操作不阻塞，outbox 积压告警，恢复后补投；
14. 登录/工具/导出/审批事件都能在 ELC 侧查到；
15. 模式标记不一致时启动失败，错误信息可操作。

### 16.3 上线门禁

- 所有 P0/P1 缺陷关闭；
- 安全测试无高危/中危未修复；
- 审批恢复、回调幂等、模式锁通过演练；
- 回滚方案在演练环境验证；
- 运维手册、告警、值班表就绪；
- 平台侧 SLO 和联系人确认。

---

## 17. middleware 就绪门禁（建议写入双方协议）

在汇智开始真实联调前，middleware 至少完成：

- [ ] SSO `/validate` 按正式协议实现：`sId`、`systemId`、`rurl`，从响应头取
      `username`/`tssotoken`；
- [ ] `UserInfoResp` 增加不可变 `userId`、`orgCode`、`enabled`；
- [ ] `/sso/login-url` 带 `systemId`、`rurl`，回调参数明确；
- [ ] MSP rights/menus 在真实环境返回预期结构，menus 支持汇智 `systemCode`；
- [ ] TCMS `/flow/start` 支持 `businessId`、案件上下文、`approvalPageURL`、
      `archiveCode`、`exempt`；
- [ ] TCMS 回调服务真正监听并注册，持久化映射，支持重试/幂等/签名；
- [ ] 回调状态语义统一：`0=通过`、`1=中止/失败`，并提供文字状态；
- [ ] 网关鉴权（mTLS/API Key）、CORS 收紧、日志脱敏；
- [ ] ELC 字段补齐，`/usr/elcLog` 持久化，ELC 侧确认收到；
- [ ] Ice 超时降到 10–30 秒，增加熔断/readiness/metrics；
- [ ] 修复测试源码并在 CI 执行；提供真实环境集成测试报告；
- [ ] 提供 OpenAPI 文档和版本变更说明。

> 这些不是“汇智能绕过去”的问题。没有它们，汇智只能做 Mock，不能上线。

---

## 18. 容易被忽略的问题清单

1. **middleware 本身还没测试**：不能把汇智的生产登录建立在未验证的网关上。
2. **“权限管理”不是二选一**：平台管身份/角色/能力包，汇智管资源画像；否则 MSP 会被
   工具/模型版本拖垮。
3. **TCMS 审批需要案件上下文**：汇智的路径/案件未必等于 TCMS 的 caseCode，必须建立
   绑定关系；没有案件的操作需要一个“无案件”流程类型。
4. **审批可能持续数小时**：现有进程内 Future 不可靠，必须持久化 + 可恢复。
5. **审批期间服务重启/多实例**：审批请求可能落到另一个实例，内存状态必须外置。
6. **身份主键不能用 loginName**：改名/调岗会让文件、数据、审计归属错乱；必须用不可变
   userId。
7. **平台登出/会话过期/用户禁用**：只做登录不做登出，会留下“平台已禁用但汇智还能用”
   的窗口。
8. **权限回收延迟**：缓存 TTL 多长、能否强制刷新，要在 ADR 里写清楚。
9. **多组织/多租户**：一套实例服务多个单位会带来数据隔离和审计边界问题，建议一单位
   一实例。
10. **平台故障降级**：新用户登录失败、老用户权限刷新失败、审批不可用，分别怎么处理，
    必须提前定义；不能自动放行。
11. **ELC 异步语义**：HTTP 200 不等于 ELC 已接收；需要 outbox、对账、告警。
12. **审计内容脱敏**：工具参数/请求体可能含敏感数据，不能原样写 ELC。
13. **回调网络可达性**：汇智在 NAT/网闸后，middleware 能否回调？需要固定出口/反向
    通道/轮询兜底。
14. **回调安全**：没有签名/时间戳/nonce，任何人都能伪造“审批通过”。
15. **模式锁定与迁移**：现有本地用户怎么映射、旧数据归属谁、回滚怎么做，必须在上线前
    演练。
16. **测试环境**：需要真实 MSP/TCMS/ELC 测试账号和测试案件，否则只能 Mock。
17. **契约版本化**：middleware 字段/行为变化要有版本号和变更通知，不能悄悄改。
18. **时钟同步**：审批超时、回调签名、token 过期都依赖时间；NTP 要纳入巡检。
19. **谁审批 AI 发起的操作**：定时任务、后台任务、智能体自动执行时，审批人是谁、
    用户不在线怎么办、超时怎么办。
20. **代理/委派**：A 委托 B 操作、服务账号代操作时，审计记录谁？需要 on-behalf-of 语义。
21. **数据导出/DLP**：审批通过后导出到本地，是否需要水印、脱敏、下载次数限制。
22. **合规证据链**：审批记录、ELC 日志、操作日志能否对得上，能否应对审计。
23. **运维 SLO**：middleware 不可用谁负责、多久恢复、汇智如何降级，需要双方约定。
24. **版本兼容矩阵**：多台汇智服务器可能接不同版本 middleware，升级顺序要约定。

---

## 19. 明确不做

- 不做运行中切换 `local`/`platform`；
- 不把每个工具/技能/模型都注册成 MSP 权限码；
- 不在 TCMS 不可用时自动通过审批；
- 不让平台角色自动获得跨用户文件/数据权限；
- 不阻塞用户请求等待 ELC 写入；
- 不把平台 token 长期放在 localStorage 或 URL；
- 不沿用 qhdHD/ELC 现有系统编码；
- 不在没有真实联调的情况下宣称“平台对接完成”。

---

## 20. 附录：middleware 接口与汇智用途

| middleware 接口 | 方法 | 汇智用途 | 备注 |
| --- | --- | --- | --- |
| `/api/v1/sso/login-url` | POST | 获取 SSO 登录 URL | 需补 `systemId`/`rurl` |
| `/api/v1/sso/validate` | POST | 校验 SSO token，获取用户 | 需修协议 |
| `/api/v1/sso/user/{loginName}` | GET | 用户信息 | 建议改用 userId |
| `/api/v1/sso/org/{loginName}` | GET | 组织信息 | |
| `/api/v1/sso/roles/{loginName}` | GET | 角色列表 | |
| `/api/v1/msp/user/{userCode}/rights` | GET | 权限码列表 | 能力映射输入 |
| `/api/v1/msp/user/{userCode}/rights/{rightsCode}` | GET | 单权限检查 | 可用于刷新 |
| `/api/v1/msp/user/{userCode}/menus?systemCode=` | GET | 菜单列表 | 可选 |
| `/api/v1/msp/user/{userCode}/menu-tree?systemCode=` | GET | 菜单树 | 可选 |
| `/api/v1/tcms/flow/start` | POST | 发起审批 | 当前不可用 |
| `/api/v1/tcms/flow/{taskId}/status` | GET | 查询状态 | 语义待核对 |
| `/api/v1/tcms/flow/callback` | POST | middleware 内部回调 | 不是汇智入口 |
| `/api/v1/elc/log` | POST | 写审计日志 | |
| `/api/v1/elc/logs/batch` | POST | 批量审计 | |
| `/api/v1/elc/log/login` | POST | 登录日志 | |
| `/api/v1/elc/log/operation` | POST | 操作日志 | |

---

## 21. 建议的下一步

1. 评审本文第 3 节 D1–D10，形成 ADR；
2. 把第 17 节门禁清单发给 middleware 团队，确认责任人和时间；
3. 确认 TCMS 是否支持“无案件”的通用审批流；如果不支持，调整审批范围；
4. 确认平台是否有不可变 `userId`；如果没有，要求 middleware 补；
5. 确定测试环境和真实测试账号；
6. 评审能力包/资源画像/审批策略初稿；
7. 通过后再启动 Phase 1（汇智内部抽象），不要先写平台客户端。

---

> 本文只做规划，不包含代码改动。后续实施以 ADR 和双方确认的契约为准。
