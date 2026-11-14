# 联调测试台（Integration Console）

运行在 **huizhi-assistant-v2.2.1 Python 运行镜像** 里的 Flask Web 程序，用来联调 middleware 的
**统一认证 / MSP / TCMS / ELC** 能力。后续新增模块（Matrix 等）按同样结构扩展。

只使用 huizhi 镜像已有的 **Flask / requests**，不需要 pip 联网安装。

已在 `huizhi-runtime:2.2.1-amd64` 内跑通完整链路（37 项自动检查）：
登录跳转 → SSO 回调 → 平台会话 → 用户/角色/组织 → 权限/单权限校验 → 菜单树 →
案件列表/详情 → 档案列表/已审批/详情 → 目标对象 → 组织案件 → 流程状态/更新 →
ELC 单条/登录/操作/批量日志 → 原始 API → 登出。

---

## 1. 目录结构

```text
msp-test-web/
├── app.py                    # 入口（create_app + 启动）
├── console/
│   ├── __init__.py           # Flask 应用工厂、模板过滤器、路由注册
│   ├── config.py             # 环境变量、模块注册表（决定左侧导航）
│   ├── http.py               # middleware HTTP 客户端、响应解析、脱敏
│   ├── services.py           # 认证 / MSP / TCMS / ELC 业务调用
│   ├── store.py              # 服务端会话（Cookie 只放 sid）
│   └── routes/
│       ├── home.py           # 控制台首页
│       ├── auth.py           # 统一认证：登录跳转、token、回调、登出
│       ├── msp.py            # 用户 / 权限 / 菜单 / 角色 / 组织 / 系统
│       ├── tcms.py           # 案件 / 档案 / 目标 / 组织案件 / 流程
│       ├── elc.py            # 审计日志：单条 / 登录 / 操作 / 批量
│       ├── tools.py          # 原始 API 调试台
│       └── settings.py       # 连接设置
├── templates/                # base.html + 各模块页面
├── static/style.css          # 本地样式，无 CDN
├── tests/
│   ├── mock_middleware.py    # 假 middleware，覆盖 MSP/TCMS/ELC
│   └── smoke_test.py         # 37 项端到端检查
├── Dockerfile
├── docker-compose.yml
├── run.sh
└── .env.example
```

---

## 2. 快速启动

前提：宿主机已加载 `huizhi-runtime:2.2.1-amd64`（x86_64）或 `huizhi-runtime:2.2.1-arm64`（ARM64）。

```bash
cd msp-test-web
cp .env.example .env
vi .env                      # 至少确认 MIDDLEWARE_BASE_URL

./run.sh
```

或：

```bash
docker compose up -d
docker compose logs -f
```

打开：

```text
http://<服务器内网IP>:18080
```

ARM64 服务器：

```bash
BASE_IMAGE=huizhi-runtime:2.2.1-arm64 ./run.sh
```

`run.sh` 使用 `--network host`，因此：

- 测试台可直接访问宿主机 `127.0.0.1:9089` 上的 middleware；
- 浏览器和 SSO 可通过 `<服务器内网IP>:18080` 访问回调。

如果 middleware 在别的机器：

```bash
MIDDLEWARE_BASE_URL=http://12.11.116.12:9089 ./run.sh
```

如果 middleware 在另一个 Docker 网络：

```bash
docker run --rm --network <middleware网络> \
  -e MIDDLEWARE_BASE_URL=http://middleware:9089 \
  -e MSP_TEST_PORT=18080 \
  -v "$PWD:/opt/msp-test-web:ro" \
  --entrypoint python \
  huizhi-runtime:2.2.1-amd64 /opt/msp-test-web/app.py
```

> 浏览器必须用**服务器内网 IP** 访问，不能用 `127.0.0.1`，否则 SSO 回跳到不了测试台。

---

## 3. 构建独立镜像

```bash
docker build --platform linux/amd64 --network none \
  --build-arg BASE_IMAGE=huizhi-runtime:2.2.1-amd64 \
  -t integration-console:1.0.0 .
```

基础层直接复用本地 huizhi 镜像，不联网。

```bash
docker run --rm --network host \
  -e MIDDLEWARE_BASE_URL=http://127.0.0.1:9089 \
  -e MSP_TEST_SYSTEM_CODE=middleware \
  integration-console:1.0.0
```

---

## 4. 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MIDDLEWARE_BASE_URL` | `http://127.0.0.1:9089` | middleware 地址；页面“连接设置”可临时改 |
| `MSP_TEST_SYSTEM_CODE` | `middleware` | MSP 菜单/权限 systemCode |
| `MSP_TEST_TCMS_SYSTEM_CODE` | 跟随 MSP | TCMS 流程/档案查询用的 systemCode |
| `MSP_TEST_ELC_SYSTEM_CODE` | 跟随 MSP | ELC clientSystemCode（展示用） |
| `MSP_TEST_PORT` | `18080` | 测试台端口 |
| `MSP_TEST_SSO_LOGOUT_URL` | 空 | SSO 登出地址，配置后首页出现 SSO 登出 |
| `MSP_TEST_VERIFY_TLS` | `false` | HTTPS 自签证书时保持 false |
| `MSP_TEST_TIMEOUT` | `20` | 调 middleware 的超时秒数 |
| `MSP_TEST_SECRET_KEY` | 内置测试值 | Flask cookie 签名密钥，多人使用请改 |
| `MSP_TEST_STATE_TTL` | `28800` | 服务端会话保留秒数 |
| `MSP_TEST_HOST` | `0.0.0.0` | 监听地址 |
| `MSP_TEST_DEBUG` | `false` | Flask debug |

---

## 5. 页面与接口对应

### 5.1 统一认证

| 页面操作 | middleware 接口 |
|---|---|
| 只获取登录 URL | `POST /api/v1/sso/login-url` |
| 直接跳转 SSO | 同上，然后 302 |
| 手工 tssotoken 建会话 | `POST /api/v1/platform/session` |
| 只校验 token | `POST /api/v1/sso/validate` |
| 按 loginName 加载 | `/api/v1/msp/users/by-login/{loginName}` + rights + menus |
| SSO 回调 | `/auth/callback` 接收 `tssotoken` / `username` |
| 登出 | 清理本地会话；可选跳 `MSP_TEST_SSO_LOGOUT_URL` |

### 5.2 MSP 平台

| 页面 | 接口 |
|---|---|
| 用户信息 | `GET /api/v1/sso/user/{loginName}`（失败回退 `/msp/users/by-login/`） |
| 权限 | `GET /api/v1/msp/user/{loginName}/rights` |
| 单权限校验 | `GET /api/v1/msp/user/{loginName}/rights/{rightsCode}` |
| 菜单树/明细 | `GET /api/v1/msp/user/{loginName}/menus?systemCode=` |
| 角色 | `GET /api/v1/sso/roles/{loginName}` |
| 组织 | `GET /api/v1/sso/org/{loginName}` |
| 系统 / 组织列表 | `GET /api/v1/msp/systems`、`/api/v1/msp/orgs` |

### 5.3 TCMS 案管

| 页面 | 接口 |
|---|---|
| 案件查询 | `GET /api/v1/tcms/user/{userId}/cases?caseKind=&caseStatus=` |
| 案件详情 | `GET /api/v1/tcms/case/{caseCode}` 或 `/api/v1/tcms/cases/id/{caseId}` |
| 档案列表 | `GET /api/v1/tcms/cases/{caseId}/archives?systemCode=&archiveKindCode=` |
| 已审批档案 | `GET /api/v1/tcms/cases/{caseId}/archives/approved` |
| 档案详情 | `GET /api/v1/tcms/cases/{caseId}/archives/{archiveCode}` |
| 目标对象 | `GET /api/v1/tcms/cases/{caseId}/targets?userId=` |
| 组织案件 | `GET /api/v1/tcms/cases/{caseId}/org?userId=&orgId=&status=` |
| 流程状态 | `GET /api/v1/tcms/flow/{taskId}/status` |
| 流程关联档案 | `GET /api/v1/tcms/flow/{flowId}/archive` |
| 更新流程状态 | `PUT /api/v1/tcms/flow/{archiveCode}/status` |
| 发起流程 | `POST /api/v1/tcms/flow/start`、`/flow/start-common` |

案件页会直接显示 middleware 返回的 `caseId`，点“档案/目标”自动带入。

常见 `archiveKindCode`：
`1217` 数据查询、`1218` 数据分析、`1219` 数据监测、`139` 特定对象、`400` 数据比对。

### 5.4 ELC 审计

| 页面 | 接口 |
|---|---|
| 单条日志 | `POST /api/v1/elc/log`（JSON） |
| 登录日志 | `POST /api/v1/elc/log/login?loginName=&clientIp=` |
| 操作日志 | `POST /api/v1/elc/log/operation?loginName=&module=...` |
| 批量日志 | `POST /api/v1/elc/logs/batch`（JSON 数组） |

### 5.5 原始 API

任意 method + path + query + JSON body，显示状态码、耗时、完整响应。内置 MSP/TCMS/ELC 常用路径预设。

---

## 6. 无平台环境自测

```bash
# 终端 1：假 middleware（19089）
python tests/mock_middleware.py

# 终端 2：测试台（18080）
MIDDLEWARE_BASE_URL=http://127.0.0.1:19089 python app.py

# 终端 3：自动冒烟
python tests/smoke_test.py
```

输出 `SMOKE TEST PASS` 表示 37 项检查全部通过。

也可以直接用容器一次性跑：

```bash
docker run --rm --network none \
  -v "$PWD:/opt/msp-test-web:ro" \
  --entrypoint sh huizhi-runtime:2.2.1-amd64 -c '
  cd /opt/msp-test-web
  python tests/mock_middleware.py & \
  MIDDLEWARE_BASE_URL=http://127.0.0.1:19089 python app.py & \
  sleep 4 && python tests/smoke_test.py'
```

---

## 7. 排查

### 7.1 连接被拒

```
ConnectionError: HTTPConnectionPool(host=..., port=...): Connection refused
```

middleware 没在该端口监听。确认：

```bash
curl -v http://<middleware>:<port>/api/v1/msp/systems
```

`application-dev.yml` 是 11089，`application.yml` / prod / test 是 9089，别填错。

### 7.2 登录 URL 404 或多一层 `/msp/`

middleware 拼接方式是 `ssoServer + loginEndpoint + ?targetUrl=`。如果 `ssoServer` 已带 `/msp/`
而 endpoint 又是 `msp/sso/login.jsp`，会出现 `/msp/msp/...`。页面“只获取登录 URL”会原样展示拼接结果。

### 7.3 回调参数缺失

SSO 没有把 token 拼回 targetUrl。检查 targetUrl 是否是浏览器实际访问的地址，以及 SSO 是否要求 `rurl`。

### 7.4 案件查不到

- `userId` 必须是 `MspUser.id`，不是 loginName；
- 该用户需有案件权限；
- `caseStatus` 默认 `1`，过滤过严会查不到；
- `systemCode` 必须是 middleware 在 MSP/TCMS 注册的编码。

### 7.5 菜单/权限报 `ExceptionInInitializerError`

这是 middleware 侧的老问题：`module-msp` 的 `MspMenuService` / `MspRightsService` 在
`<clinit>` 里取 `SpringContextHolder.getApplicationContext().getBean(SystemConfigurer.class)`。
`zuixin` 已通过 `@Import(SystemConfigurer, SpringContextHolder)` + `custom.properties`
解决；旧包需要打 `MspCompatConfig` 补丁，并统一 log4j2 日志栈。

### 7.6 会话丢失

测试台用内存保存会话，**只能单进程运行**。请用 `python app.py` 或 `gunicorn -w 1`，
不要多 worker。重启测试台会丢会话。

---

## 8. 安全说明

- 内网联调工具，自身无登录/鉴权，不要暴露公网。
- `tssotoken` 只存内存，不写日志；页面显示打码。
- Flask cookie 只存随机 sid，不存 token/菜单/案件数据。
- “原始 API”可访问任意路径（有 SSRF 能力），仅限可信内网。
- 多人共用请设置 `MSP_TEST_SECRET_KEY` 并限制来源。
