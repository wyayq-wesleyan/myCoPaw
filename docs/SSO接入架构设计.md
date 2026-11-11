# SSO 接入架构设计与改造评估

- 日期：2026-11-05
- 范围：认证（登录）与授权/隔离（登录后）的全链路
- 结论：**可以改造，且不需要动数据模型与隔离逻辑**。核心是把"证明你是谁"抽象成可切换的
  AuthProvider，登录后继续用内部令牌与本地账号记录。

---

## 0. 结论摘要

### 0.1 你的判断成立的部分

| 判断 | 评估 |
| --- | --- |
| 登录前可切换（自主用户系统 / SSO） | ✅ 成立。登录前只做"凭据校验 + 建立会话"，是天然的替换点 |
| 登录后应解耦、不受影响 | ✅ 基本成立。登录后只依赖 `username + role`，不关心密码怎么来的 |
| 用配置文件选择不同登录方式 | ✅ 可行。现有配置体系（`config.json` / env）可以直接扩展 `auth.mode` |

### 0.2 但有三处"登录后仍在读本地用户库"，必须一起改

| # | 位置 | 问题 | SSO 下的后果 |
| --- | --- | --- | --- |
| **C1** | `auth.py:368` `has_registered_users()` + `_should_skip_auth` | "有没有本地用户"被当作"要不要鉴权" | SSO 模式下本地还没有用户 → **整个 `/api/*` 跳过鉴权**（严重） |
| **C2** | `user_space.py:132` `is_admin_user()` 读 `get_user_info()`，异常时回退 `_slug(username)=="admin"` | 管理员判断依赖本地记录 + 用户名特例 | SSO 用户若叫 `admin`，即使角色映射为 user 也会被当管理员 |
| **C3** | `user_space.py` 多处 `list_users()`（迁移、修复、管理员智能体选择） | 登录后的批处理任务枚举"本地用户" | SSO 用户不会被纳入；JIT 之前这些任务看不到他们 |

另外两个必须处理的点：

- **用户名是全部数据的键**：工作区目录哈希、每账号数据库名、知识 `owner_id`、`owner_username`、
  模型/工具/技能/菜单权限，全部以 username 为键。而 `update_credentials` 改用户名时
  **只改 auth.json，不迁移任何数据**（`auth.py:604-653`）。SSO 用户名若可变，数据会"丢"。
- **`verify_token` 要求本地用户记录存在且 enabled**（`auth.py:173-209`）。SSO 用户必须先
  JIT 建档，否则登录成功但后续每个请求 401。

### 0.3 推荐方案（一句话）

> **SSO 只负责"证明你是谁"；登录成功后仍然签发内部令牌、仍然落一条本地账号记录
> （JIT 建档，无密码）。登录后的权限、隔离、数据键完全不动。**

这样改动集中在"登录前 + 账号供给"，不会碰到工作区/数据库/知识/路径/权限这些核心逻辑。

---

## 1. 现状链路（代码位置）

### 1.1 登录前

```
POST /api/auth/login {username,password}
  └─ routers/auth.py:275 → auth.py:661 authenticate()
       ├─ _load_auth_data() 读 ~/.qwenpaw.secret/auth.json
       ├─ verify_password(单轮加盐 SHA-256)
       └─ create_token(username)  HMAC-SHA256，jwt_secret 存在 auth.json
  └─ 前端 api/modules/auth.ts → localStorage(qwenpaw_auth_token)
```

| 能力 | 位置 | SSO 下是否保留 |
| --- | --- | --- |
| 用户名/密码登录 | `routers/auth.py:275` | ❌ 由 SSO 替代 |
| 首次注册（无用户时） | `routers/auth.py:335` | ❌ 关闭 |
| 修改密码 / 管理员重置 | `routers/auth.py:601`、`update-profile` | ❌ 关闭（SSO 管密码） |
| 自动注册管理员（env） | `auth.py:560 auto_register_from_env` | ❌ 仅 local 模式 |
| 状态查询 | `routers/auth.py:373` | ✅ 扩展返回 `mode` |
| 令牌签发/校验 | `auth.py:133 create_token` / `auth.py:173 verify_token` | ✅ **继续使用** |

### 1.2 登录后

```
AuthMiddleware(auth.py:875)
  ├─ _should_skip_auth()：未启用鉴权 / 无本地用户 → 放行
  ├─ verify_token()：验签、exp、撤销表、用户存在且 enabled
  └─ request.state.user = 用户记录
        ↓
get_request_username(request)  ← 所有路由/工具的唯一身份来源
        ↓
user_space.get_user_root(username) → workspaces/<slug>
agents.owner_username / chats.owner / paths.owner_username
knowledge X-User-Id → documents.owner_id
data_tables → 每账号独立数据库
permissions → model / skill / tool / menu（以 username 为键）
```

**关键结论**：登录后拿到的只是 `username`；只要 SSO 能稳定映射出同一个 username，
后面全部逻辑不用改。

### 1.3 数据键一览（SSO 必须保证 username 稳定）

| 数据 | 键 | 位置 |
| --- | --- | --- |
| 工作区目录 | `slug(username)` | `user_space.get_user_root` |
| 用户状态/权限 | username | `user_space`（state.json 等） |
| 每账号数据库 | username（小写 ASCII 直接用） | `data_tables/connection.py:55` |
| 智能体 | `owner_username` | `config.json agents.profiles` |
| 会话 | owner/session | `chats`、`platform.conversation_history` |
| 知识 | `owner_id` | `knowledge-service/documents.owner_id` |
| 路径/案件 | `owner_username` | `platform.paths` |
| 用量 | username | token_usage |

---

## 2. 登录前 / 登录后 切分（按你的思路整理）

### 2.1 登录前（可替换）

| 职责 | 归属 | 说明 |
| --- | --- | --- |
| 凭据校验 | AuthProvider | local：密码；SSO：跳转/回调/接口校验 |
| 建立本地会话 | 共用 | 统一签发内部 token |
| 账号建档 | 共用（JIT） | SSO 首次登录自动建本地记录 |
| 密码/注册 | local only | SSO 模式关闭 |
| 登录页形态 | 配置驱动 | local 表单 / SSO 按钮 |

### 2.2 登录后（不动）

| 职责 | 归属 |
| --- | --- |
| 令牌校验、身份解析 | 现有 `AuthMiddleware` |
| 权限（模型/工具/技能/菜单） | 现有 `user_space` |
| 隔离（工作区/库/知识/路径） | 现有逻辑 |
| 审计/用量 | 现有逻辑 |

### 2.3 灰色地带（必须改，但不改登录后语义）

| 点 | 现状 | 改法 |
| --- | --- | --- |
| C1 无用户时跳过鉴权 | `has_registered_users()` | 改为 `auth_configured()`：只要启用了鉴权（local 有用户 / SSO 已配置），就必须鉴权 |
| C2 管理员判断 | 读 auth.json + `admin` 特例 | 只读本地账号记录的 `role`（JIT 后存在）；**删除字符串特例** |
| C3 批处理枚举用户 | `list_users()` | 抽象成 `list_accounts()`，SSO 用户同样在册 |
| 改用户名 | `update_credentials` 只改 auth.json | SSO 模式**禁止改用户名**；如必须改，走迁移工具（工作区/DB/owner 全量重命名） |
| 自动注册 | `auto_register_from_env` | 仅 local 模式 |
| 回环免鉴权 | `allow_no_auth_hosts` | SSO 模式保持为空，避免绕过 |

---

## 3. 目标架构

### 3.1 AuthProvider 抽象

```python
class AuthProvider(Protocol):
    mode: Literal["local", "sso"]

    async def status(self) -> AuthStatus          # 前端用它决定登录页
    async def authenticate(self, request) -> Principal | None
    def capabilities(self) -> Capabilities        # password/register/rename/self_service
    async def provision(self, principal) -> None  # JIT 建档（SSO）
```

- `LocalAuthProvider`：包住现有 `authenticate` / `create_user_account`，行为不变。
- `SsoOidcProvider`：OIDC 授权码 + PKCE + state/nonce。
- `SsoApiProvider`：适配"调用对方接口校验 ticket/token"的自定义模式。
- `SsoSamlProvider` / `SsoCasProvider`：如客户是这两种协议再加。

### 3.2 配置（建议放在 `config.json` 的 `auth` 段）

```json
{
  "auth": {
    "mode": "local",
    "token_ttl_seconds": 604800,
    "allow_local_fallback": true,
    "sso": {
      "protocol": "oidc",
      "issuer": "https://sso.example.gov.cn",
      "client_id": "huizhi-assistant",
      "client_secret_ref": "env:SSO_CLIENT_SECRET",
      "scopes": ["openid", "profile", "email"],
      "redirect_uri": "https://huizhi.example.gov.cn/api/auth/sso/callback",
      "username_claim": "preferred_username",
      "display_name_claim": "name",
      "role_claim": "groups",
      "role_mapping": {
        "huizhi-admins": "admin",
        "huizhi-users": "user"
      },
      "default_role": "user",
      "jit_provision": true,
      "sync_on_login": true
    }
  }
}
```

环境变量覆盖：`QWENPAW_AUTH_MODE=sso`、`QWENPAW_SSO_ISSUER` 等，便于容器部署。

### 3.3 OIDC 登录流程（推荐）

```
浏览器                   后端                           IdP
  │  GET /auth/sso/start   │                             │
  │───────────────────────▶│ 生成 state/nonce/PKCE       │
  │  302 到 IdP 授权页     │                             │
  │──────────────────────────────────────────────────────▶│
  │                        │◀── 302 /callback?code&state ─│
  │                        │ 校验 state / 换 token / 验签 │
  │                        │ 取 userinfo → 映射 username/role
  │                        │ JIT 建档（无密码，auth_source=sso）
  │                        │ create_token(username)
  │◀── 302 前端 /login/callback#token=... 或 set-cookie ──│
  │  存 token → 进入控制台（登录后逻辑完全不变）          │
```

要点：
- **不把 IdP 的 access token 透传给前端**；前端只拿内部 token。
- state/nonce/PKCE 必须校验；`iss/aud/exp/nbf` 必须校验；时钟偏差容忍 ≤ 2 分钟。
- 回调失败返回明确错误页，**绝不降级为无鉴权**。

### 3.4 自定义 API 模式（如果对方只提供接口）

```
POST /api/auth/sso/login { ticket }   （或 POST /auth/sso/exchange）
  └─ 后端调 SSO：POST {sso.userinfo_url} with ticket
       ├─ 200 {user_id, username, display_name, roles}
       └─ 401/超时 → 返回 401/503，记录审计
  └─ 映射 → JIT → create_token
```

适用：对方已有"校验 ticket 返回用户信息"的接口，或用户名密码由对方接口校验。
**不建议**由我们接收并转发密码（除非对方明确要求且链路是内网 TLS）。

### 3.5 账号记录扩展（向后兼容）

`auth.json` 的 `users.<username>` 增加字段：

| 字段 | 说明 |
| --- | --- |
| `auth_source` | `local` / `sso` |
| `external_id` | SSO 的 `sub`（稳定唯一键） |
| `display_name` | 显示名 |
| `last_login_at` | 最后登录 |
| `disabled_by_idp` | 对方已禁用 |
| `password_hash/salt` | SSO 用户为空 |

现有记录没有这些字段 → 默认 `auth_source=local`，升级无感。

### 3.6 角色与权限

| 问题 | 建议 |
| --- | --- |
| 角色从哪来 | SSO claim → `role_mapping`；未命中用 `default_role` |
| 权限从哪来 | **仍由本地管理员授权**（模型/工具/技能/菜单）；SSO 只给角色 |
| JIT 默认权限 | 普通用户默认无模型授权（与现有一致），管理员登录后补授权 |
| 管理员 | 角色映射为 `admin` 的 SSO 用户；**同时保留一个本地 break-glass 管理员** |

### 3.7 降级与 break-glass（必须有）

- SSO 不可用：返回 503 + 明确文案，**不能**自动变成"免鉴权"；
- `allow_local_fallback=true` 时，本地管理员账号仍可登录（用于 SSO 故障/初始化）；
- 本地 fallback 账号必须强口令、单独审计、可禁用。

### 3.8 登出

- 本地：`revoke_token`（现有）+ 清前端 token；
- 可选：跳转 IdP 的 RP-initiated logout（`end_session_endpoint`）；
- SSO 侧登出后，本地 token 仍有效到过期——如要求"单点登出即时生效"，需要短 TTL + 每次请求校验 SSO 会话（成本高），或接受窗口期。

---

## 4. 改造清单

### 4.1 后端

| 文件 | 改动 |
| --- | --- |
| 新增 `app/auth_providers/` | `base.py`（Protocol）、`local.py`、`sso_oidc.py`、`sso_api.py` |
| `app/auth.py` | `is_auth_enabled` → `auth_mode()`；`has_registered_users` → `auth_configured()`；`is_admin_user` 去 auth.json 依赖、删除 `admin` 特例；`auto_register_from_env` 仅 local；`update_credentials` 在 SSO 下禁止改用户名/密码；`verify_token` 保持 |
| `app/routers/auth.py` | 新增 `/auth/sso/start`、`/auth/sso/callback`、`/auth/sso/login`；`/auth/status` 返回 `mode`、`login_url`、`capabilities`；password/register 在 SSO 下 403 |
| `app/user_space.py` | `list_users` 调用统一为 `list_accounts()`；`is_admin_user` 走账号记录 |
| `app/middleware` | `_should_skip_auth` 改为基于 `auth_configured()` |
| `config/config.py` | 新增 `auth` 段（含 sso 子段） |
| `deploy/entrypoint.sh` | `allow_no_auth_hosts` 仅在 local 模式清空；SSO 模式保持空列表 |
| 审计 | 登录成功/失败、SSO sub、映射 username、角色、失败原因 |

### 4.2 前端

| 文件 | 改动 |
| --- | --- |
| `pages/Login/index.tsx` | 按 `/auth/status.mode` 渲染：local 表单 / "SSO 登录"按钮；隐藏注册 |
| `pages/Login/SsoCallback.tsx`（新增） | 接收 token（hash 或 cookie）→ 存 → 跳转 |
| `layouts/Header.tsx` | `capabilities.password=false` 时隐藏"修改密码" |
| `pages/Settings/UserManagement` | SSO 模式隐藏建号/改密/重置；保留授权与禁用 |
| `api/modules/auth.ts` | 新增 sso start/login 方法；status 类型扩展 |

### 4.3 数据

- `auth.json` 用户记录加字段（自动兼容）；
- 不改任何业务数据的键；
- 若 SSO 用户名与现有本地用户名不一致：JIT 新建（不合并），管理员决定是否迁移。

---

## 5. 迁移与回滚

| 阶段 | 动作 |
| --- | --- |
| 0 | 默认 `mode=local`，升级无感；补 `auth` 配置段 |
| 1 | 接入 SSO（测试环境）：mock IdP 跑通登录/回调/JIT/角色映射 |
| 2 | 灰度：`mode=sso` + 本地 break-glass 管理员；先让管理员账号走 SSO |
| 3 | 全量：关闭本地注册与密码入口；保留 fallback |
| 回滚 | `mode=local` 即可；SSO 用户记录保留（无密码，无法本地登录，但数据不丢） |

---

## 6. 测试计划（关键用例）

| 类别 | 用例 |
| --- | --- |
| Local 回归 | 现有登录/注册/改密/权限测试全部通过 |
| SSO 正常 | 授权码 → 回调 → JIT → token → 登录后权限/隔离与本地用户一致 |
| SSO 安全 | state 不匹配、nonce 重放、签名错误、iss/aud 错误、过期、时钟偏差 → 拒绝 |
| 账号 | JIT 只建一次；角色映射命中/未命中；对方禁用 → 登录失败；本地 fallback 可用 |
| 隔离 | SSO 用户 A 不能访问 B 的工作区/会话/知识/表（复用现有越权用例） |
| 前端 | mode 切换、隐藏密码入口、回调成功/失败 |
| 降级 | IdP 超时/503 → 明确错误，**不跳过鉴权** |
| 变异验证 | 去掉 state 校验 → 用例失败；去掉 JIT → 登录后 401；去掉 `auth_configured` → 无用户时跳过鉴权 |

---

## 7. 工作量

| 项 | 人日 |
| --- | --- |
| Provider 抽象 + Local 适配 + 配置 | 2–3 |
| OIDC（state/nonce/PKCE、验签、JIT、角色映射） | 5–7 |
| 自定义 API 适配（如需要） | 2–3 |
| 后端端点/状态/降级/审计 | 3–4 |
| 前端登录页/回调/权限隐藏 | 3–4 |
| 测试（含 mock IdP）+ 文档 | 4–6 |
| **合计** | **约 19–27 人日（3–5 周）** |

SAMPL/国产 SSO 若协议不标准（例如私有 ticket 接口），主要成本在联调，不在代码。

---

## 8. 需要你先确认的 7 件事

1. **协议**：对方是 OIDC / SAML / CAS，还是只提供"校验 ticket 返回用户"的私有接口？
2. **唯一键**：SSO 的用户名是否稳定？能否提供不变的 `sub`/工号？
3. **角色来源**：角色由 SSO 下发（groups/roles claim），还是本地管理员授权？
4. **账号供给**：允许 JIT 自动建档，还是必须由管理员预先建号？
5. **降级**：是否允许保留本地 break-glass 管理员？
6. **登出**：是否要求"SSO 登出后本地立即失效"（影响 TTL/校验频率）？
7. **多租户/多 IdP**：一套部署只对接一个 SSO，还是每个租户不同 IdP？

这 7 项确认后，可以把 Provider 接口和配置 schema 定稿，再按第 4 节的清单实施。
