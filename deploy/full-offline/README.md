# 汇智小助手 —— 全新服务器离线部署包

面向 **一台全新的 x86_64（amd64）服务器**，含 `/data` 数据盘。整个部署过程**不需要
外网**：镜像、代码、前端产物、部署文件都在本包里。

- 版本：`2.2.1`（镜像 tag `huizhi-runtime:2.2.1-amd64`）
- 部署方式：**代码挂载**（挂载宿主机目录，改代码/发版不需要重新加载镜像）
- 全部数据落在 `/data/huizhi-assistant`（本包解压处），删掉本目录等于全新装机

---

## 一、十分钟部署（TL;DR）

压缩包约 **6.6GB**，超过很多传输通道/网盘 **1GB 的单文件上限**，因此**以分卷形式提供**：
`parts/` 目录下是若干 `.part-00`、`.part-01` … 分卷（每个 <1GB），外加一个
`join-parts.sh` 与 `.sha256`。先把分卷逐个传上服务器，合并回完整包，再解压部署。

假设服务器已装好 Docker（见 §2）、已挂载 `/data` 数据盘：

```bash
# 0) 在服务器上建一个临时目录，把 parts/ 下所有文件传进去（分卷 + join-parts.sh + .sha256）
mkdir -p /data/huizhi-download && cd /data/huizhi-download

# 1) 合并分卷并校验（脚本会自动按编号顺序合并，并核对 sha256；缺卷/损坏会明确报错）
bash join-parts.sh
#    产物：/data/huizhi-download/huizhi-assistant-full-YYYYMMDD.tar.gz

# 2) 解压到 /data 下（包里已含 huizhi-assistant/ 一级目录，解压即得该路径）
cd /data
tar -xf /data/huizhi-download/huizhi-assistant-full-YYYYMMDD.tar.gz
cd /data/huizhi-assistant

# 3) 加载镜像（约 6.6GB 归档 → 约 20GB，首次 3~10 分钟）
#    脚本会逐个校验 images/ 下的 sha256 再加载
bash scripts/load-images.sh

# 4) 改配置（★ 至少改模型地址、两个 Key、admin 密码）
vi .env
vi knowledge-service/.env

# 5) 自检（把「部署完才发现」的问题提前暴露：架构/端口/Key 一致性/占位值…）
bash scripts/preflight-check.sh

# 6) 启动（按 PG → 知识服务 → 主应用顺序，含健康等待）
bash scripts/start-all.sh
```

> **合并失败怎么办**：脚本会告诉你是"缺卷"（编号不连续）还是"内容损坏"（sha256 不匹配）。
> 两种情况都只需重新传输对应分卷再跑一次；分卷之间是独立字节片段，重传单个即可。
> 全部文件传完后也可以手工核对：分卷个数 × 每卷大小应等于 `.sha256` 文件里记录的原包大小。

确认无误后可以删掉分卷释放空间：

```bash
rm -f /data/huizhi-download/*.part-*
```

然后浏览器打开 `http://<服务器IP>:8088/`，用 `.env` 里的用户名/密码登录。

后续运维：

```bash
bash scripts/stop-all.sh --status     # 看状态
bash scripts/stop-all.sh              # 停应用+知识服务（数据库继续跑，数据不丢）
bash scripts/start-all.sh             # 重新启动
docker logs -f huizhi-assistant       # 看主应用日志
docker logs -f huizhi-knowledge-api   # 看知识服务日志
```

---

## 二、服务器前提

| 项 | 要求 | 说明 |
| --- | --- | --- |
| CPU 架构 | **x86_64 / amd64** | 本包为 amd64 构建。ARM 机器**不能用**（`scripts/preflight-check.sh` 会直接拦下） |
| Docker | 20.10+，含 `docker compose` 插件（v2） | `docker compose version` 能输出版本即可。**不是**旧版 `docker-compose`(v1) |
| zstd | 需要 | 镜像用 zstd 压缩。`apt-get install -y zstd` / `yum install -y zstd` |
| 内存 | 建议 ≥128GB | 默认上限之和约 208GB，是**上限**不是常驻占用；小内存机器务必按 §5 调低 |
| 磁盘 | `/data` 建议 ≥200GB 可用 | 分卷约 6.6GB + 解压出的镜像约 20GB；文档、图谱、备份随后续使用增长 |
| 网络 | 内网可达模型服务 | 聊天模型 + 嵌入模型（OpenAI 兼容、带 `/v1`）。服务器本身**可完全无外网** |

> 合并分卷只用 `cat` / `shasum` / `bash`，任何 Linux 发行版都自带，无需额外安装。

Docker 建议开启以下内核参数（大量并发连接时需要）：

```bash
cat >> /etc/sysctl.conf <<'EOF'
fs.inotify.max_user_watches=524288
net.core.somaxconn=1024
EOF
sysctl -p
```

---

## 三、目录结构

```
传输阶段（在服务器上合并分卷用的一次性目录，合并完可删）
/data/huizhi-download/
├── huizhi-assistant-full-YYYYMMDD.tar.gz.part-00  ← 分卷（每个 <1GB）
├── huizhi-assistant-full-YYYYMMDD.tar.gz.part-01
├── …
├── huizhi-assistant-full-YYYYMMDD.tar.gz.sha256   ← 合并后校验用
└── join-parts.sh                                  ← 合并脚本

解压后的部署目录
/data/huizhi-assistant/
├── README.md                     ← 本文件
├── .env                          ← 【要改】主应用配置（认证、端口、资源上限、知识服务地址）
├── .env.example                  ← 模板（只读参考）
├── docker-compose.yml            ← 主应用 compose（amd64）
├── entrypoint.sh                 ← 容器入口（打包时同步，勿改）
├── code-mount-entrypoint.sh      ← 代码挂载入口（打包时同步，勿改）
├── deploy/config/supervisord.conf.template
│
├── app/                          ← 主应用代码 + 前端产物（只读挂载进容器）
│   ├── src/qwenpaw/              ← Python 后端
│   └── console/dist/             ← 前端静态文件（已构建，勿手改）
│
├── images/
│   ├── huizhi-runtime-2.2.1-amd64.tar.zst   ← 应用镜像（约 6.4GB，含 MinerU 模型）
│   │   （解压后约 20GB；pgvector 归档约 150MB）
│   ├── huizhi-runtime-2.2.1-amd64.tar.zst.sha256
│   ├── pgvector-pg17-amd64.tar.zst          ← 数据库镜像
│   └── pgvector-pg17-amd64.tar.zst.sha256
│
├── knowledge-service/            ← 知识服务（独立 compose，务必进这个目录执行）
│   ├── .env                      ← 【要改】模型地址、Key、图谱开关
│   ├── .env.example
│   ├── docker-compose.yml        ← 知识服务 api + worker（amd64）
│   ├── docker-compose.postgres.yml ← PostgreSQL 17（账号库/平台库/图谱库）
│   ├── initdb/                   ← 首次初始化脚本（自动建图谱库）
│   ├── app/                      ← 知识服务代码（只读挂载）
│   └── data/                     ← 文档存储、图谱文件、日志
│
├── scripts/
│   ├── load-images.sh            ← 加载镜像（幂等）
│   ├── preflight-check.sh        ← 部署前自检（只读，不改任何东西）
│   ├── start-all.sh              ← 按顺序启动全部服务
│   └── stop-all.sh               ← 停止/查看状态
│
├── data/                         ← 全部运行数据（首次启动自动创建）
│   ├── working/                  ← 用户、工作区、会话、技能、定时任务、业务数据
│   ├── working.secret/           ← ★ 模型密钥、认证配置等敏感文件
│   ├── backups/                  ← 应用备份
│   └── logs/                     ← 应用 + 知识服务日志
│
└── knowledge-service/data/       ← 知识侧数据（首次启动自动创建）
    ├── files/                    ← 上传的原始文档
    ├── graph/                    ← 图谱工作目录（多租户模式使用）
    ├── lightrag/                 ← 旧共享图谱（仅旧模式使用）
    └── postgres-data/            ← ★ PostgreSQL 数据目录（备份它 = 备份全部结构化数据）
```

**要备份的东西只有三处**（详见 §7）：

```
data/working/            data/working.secret/           knowledge-service/data/
```

---

## 四、首次启动会发生什么

按 `scripts/start-all.sh` 启动后，日志里会依次出现下面这些**自动**动作，都只需一次、
可重复执行：

1. **建库建表**：PostgreSQL 容器初始化时创建 `huizhi_knowledge` 库，并由
   `knowledge-service/initdb/` 自动创建图谱库 `huizhi_knowledge_graph`。
2. **平台存储迁移**：主应用启动时把历史数据（会话历史、治理审计、词元用量）导入
   PostgreSQL，并建好路径隔离所需的三张表。日志：`legacy-import: ...`、
   `platform store: applied migration ...`
3. **账号与路径初始化**：为每个已有账号创建公共空间（根路径），并授予其智能体。
   日志：`path-migration: N account(s), M agent binding(s) ensured`
4. **图谱迁移**（启用多租户图谱时）：把已有文档重建进各自路径的 workspace。
   日志：`graph-migration: {...}`。这一步可能跑较久（要做实体抽取）。
5. 之后浏览器即可访问；知识服务 worker 开始处理上传的文档。

> 全新服务器上第 2~4 步几乎没有工作量（库里没数据），通常几秒内完成。

---

## 五、配置说明（`.env` 要点）

### 5.1 主应用 `.env`

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `COPAW_PORT` | `8088` | 网页访问端口 |
| `MYCOPAW_IMAGE` | `huizhi-runtime:2.2.1-amd64` | **不要改**，须与 §3 里的镜像一致 |
| `QWENPAW_AUTH_PASSWORD` | 占位 | ★**必须改**为强密码 |
| `QWENPAW_KNOWLEDGE_URL` | `http://127.0.0.1:18100` | 知识服务地址（同机无需改） |
| `QWENPAW_KNOWLEDGE_API_KEY` | 占位 | ★须与 `knowledge-service/.env` 的 `KNOWLEDGE_API_KEY` **一致**（都留空也可以） |
| `QWENPAW_DATA_TABLES_DB_*` | 本地 `15432` | 指向本地 PG，标准部署无需改 |
| `QWENPAW_MEMORY_LIMIT` / `QWENPAW_CPU_LIMIT` | `48g` / `8.0` | 按机器实际调，**给宿主机留余量** |

### 5.2 知识服务 `knowledge-service/.env`

| 变量 | 说明 |
| --- | --- |
| `CHAT_BASE_URL` / `CHAT_API_KEY` / `CHAT_MODEL` | ★聊天模型：地址须 OpenAI 兼容、带 `/v1`；模型名须与模型服务上**完全一致** |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` | ★嵌入模型。**未配置时无法上传文档**（知识服务会返回 409） |
| `EMBEDDING_DIMENSIONS` | 须与嵌入模型实际维度一致，默认 `1024` |
| `KNOWLEDGE_API_KEY` | ★与主应用一致 |
| `LIGHTRAG_URL` | **留空**（推荐：启用多租户图谱，见下） |
| `KNOWLEDGE_GRAPH_DATABASE_URL` | 多租户图谱库连接串，默认已指向本地图谱库 |
| `KNOWLEDGE_*_MEMORY_LIMIT` / `*_CPU_LIMIT` | 各容器上限，按机器调整 |

### 5.3 ★ 内存上限怎么配

两份 `.env` 的上限是**全部容器**的总和，必须小于宿主机内存：

```
总量 ≈ QWENPAW_MEMORY_LIMIT
      + KNOWLEDGE_API_MEMORY_LIMIT + KNOWLEDGE_WORKER_MEMORY_LIMIT
      + LIGHTRAG_MEMORY_LIMIT + KNOWLEDGE_POSTGRES_MEMORY_LIMIT
```

默认合计约 208GB，是给 256GB 机器的配置。**128GB 机器建议改成**：

```ini
# 主应用 .env
QWENPAW_MEMORY_LIMIT=32g
QWENPAW_CPU_LIMIT=6.0

# knowledge-service/.env
KNOWLEDGE_API_MEMORY_LIMIT=8g
KNOWLEDGE_WORKER_MEMORY_LIMIT=40g
LIGHTRAG_MEMORY_LIMIT=16g
KNOWLEDGE_POSTGRES_MEMORY_LIMIT=16g
```

（若 `LIGHTRAG_URL` 留空，`LIGHTRAG_MEMORY_LIMIT` 对应的容器不会启动，可不计。）

### 5.4 关于知识图谱（重要，二选一）

| 选择 | 配置 | 特点 |
| --- | --- | --- |
| **推荐：多租户图谱** | `LIGHTRAG_URL=`（留空） | 知识服务**进程内**直连 PostgreSQL，**每条路径一个 workspace**，隔离由数据库主键保证：跨案件读不到彼此。图谱不额外占容器 |
| 旧版共享图谱 | `LIGHTRAG_URL=http://lightrag:9621` | 单一共享图谱 + 应用层过滤，隔离强度较低。仅排查老问题时使用，且要额外给 `LIGHTRAG_*` 内存 |

多租户模式首次启动会自动建库建表（见 §4 第 1、4 步）。若 PostgreSQL **数据目录已存在**
（例如复用旧的 `postgres-data/`），`initdb/` 不会执行，需要手工建库一次：

```bash
docker exec huizhi-knowledge-postgres \
  psql -U huizhi_knowledge -d huizhi_knowledge \
  -c "CREATE DATABASE huizhi_knowledge_graph OWNER huizhi_knowledge;"
```

---

## 六、验证部署成功

```bash
bash scripts/stop-all.sh --status      # 四个容器都应 Up (healthy)

# 应用健康
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/          # 期望 200
# 知识服务健康
curl -s http://127.0.0.1:18100/health | head -c 300                     # 期望 "upload_ready": true
```

浏览器登录后建议按顺序确认：

1. 左侧菜单出现「知识空间」「数据空间」两页；两页左侧的路径栏里都有
   **「公共区（未归类）」** 节点（这是账号的公共空间，新建账号也一定会有）。
2. 聊天页顶部模型选择可用（能看到管理员授权的模型）。
3. 上传一个小文件到知识空间；状态从「正在解析」变为「已入库」；用关键词搜索能命中。
4. 数据空间里新建一张表并导入一个小 CSV，能预览到数据。

---

## 七、备份与恢复

### 备份（三处 + 一个数据库导出）

```bash
cd /data/huizhi-assistant
D=$(date +%Y%m%d-%H%M%S)

# 1) 结构化数据（账号、会话、路径、知识元数据、图谱）—— 最重要
docker exec huizhi-knowledge-postgres pg_dumpall -U huizhi_knowledge \
  | gzip > /data/backup-hz-pg-$D.sql.gz

# 2) 文件与配置
tar -czf /data/backup-hz-files-$D.tar.gz \
  data/working data/working.secret knowledge-service/data/files \
  knowledge-service/data/graph .env knowledge-service/.env
```

> PostgreSQL 数据目录（`knowledge-service/data/postgres-data/`）**不要在运行中直接
> tar**，用上面的 `pg_dumpall` 更可靠。

### 恢复

```bash
bash scripts/stop-all.sh --with-db
# 恢复文件
tar -xzf /data/backup-hz-files-*.tar.gz -C /data/huizhi-assistant
# 恢复数据库（先起 PG，再灌数据）
(cd knowledge-service && docker compose -f docker-compose.postgres.yml up -d)
gunzip -c /data/backup-hz-pg-*.sql.gz | \
  docker exec -i huizhi-knowledge-postgres psql -U huizhi_knowledge -d postgres
bash scripts/start-all.sh
```

> 从**另一台机器**的备份恢复时，光恢复 `data/working/` 不够：用户、认证、密钥都在
> PostgreSQL 里，必须一起恢复 §7 的 `pg_dumpall`。

---

## 八、升级（发新版本代码时）

**代码挂载模式的升级不需要重新加载镜像**：替换 `app/` 与 `knowledge-service/app/`
后重启容器即可。用 `huizhi-code-update-YYYYMMDD.tar.gz` 这类代码包：

```bash
cd /data/huizhi-assistant

# 1) 备份（务必先做，见 §7）
# 2) 解开代码包到临时目录，再覆盖
tar -xzf huizhi-code-update-YYYYMMDD.tar.gz -C /tmp/hz-new
cp -a /tmp/hz-new/app/. app/
cp -a /tmp/hz-new/knowledge-service/app/. knowledge-service/app/

# 3) 重启
bash scripts/start-all.sh
```

启动时会自动应用数据库结构变更与数据迁移（见 §4），**不需要人工执行 SQL**。
升级前请确认新版本的说明里是否有「必须重启知识服务」等额外动作。

> 若升级涉及**底座依赖**变化（PyTorch / MinerU / LightRAG / Dockerfile），会另发一个
> 新 tag 的 `huizhi-runtime` 镜像，那时才需要 `load-images.sh` + 改 `.env` 的
> `MYCOPAW_IMAGE`。

---

## 九、常见问题

**Q1. `preflight-check.sh` 报「CPU 架构不是 x86_64」**
本包是 amd64 专用。ARM 服务器（含 Apple Silicon）需要另出 arm64 包。

**Q2. 启动后打不开网页**
先看 `bash scripts/stop-all.sh --status` 与 `docker logs --tail 80 huizhi-assistant`。
最常见原因是 `.env` 的 `COPAW_PORT` 与别的服务冲突（自检会提前提示）。

**Q3. 知识服务起来但上传文档报 409 / 「缺少嵌入模型配置」**
`knowledge-service/.env` 的 `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` 没填或填错。
用下面的命令直接测模型服务是否可达：

```bash
curl -s http://127.0.0.1:18100/health | python3 -m json.tool | grep -A5 embedding
```

**Q4. 检索不到知识空间的内容**
确认四件事：①文件状态已「已入库」；②知识服务 worker 在运行
（`docker ps | grep knowledge-worker`）；③当前智能体挂载了资料所在的路径
（聊天页顶部「知识空间」选择器）；④资料在「公共区」而智能体**没有**挂载公共区时
是读不到的 —— 检查该选择器里「公共区（未归类）」是否勾选。

**Q5. 换了服务器，知识空间是空的**
`data/working/` 与 PostgreSQL 都要一起迁移（§7）。只搬文件不搬数据库会丢用户与元数据。

**Q6. 日志在哪**
宿主机 `data/logs/`（应用）与 `knowledge-service/data/logs/`（知识服务、含
`knowledge.log`、`lightrag.log` 等）。

**Q7. 想彻底重来**
停掉全部容器（`--with-db`），删除 `data/` 与 `knowledge-service/data/`，再按 §1 启动。
**注意这会删掉所有用户数据。**

---

## 十、组件与端口一览

| 容器 | 镜像 | 端口（仅本机回环） | 作用 |
| --- | --- | --- | --- |
| `huizhi-assistant` | `huizhi-runtime:2.2.1-amd64` | `8088`（网页） | 主应用：网页、API、智能体运行 |
| `huizhi-knowledge-api` | 同上 | `18100` | 知识服务 API（文档、检索） |
| `huizhi-knowledge-worker` | 同上 | 无 | 解析、嵌入、实体抽取（后台） |
| `huizhi-knowledge-postgres` | `pgvector/pgvector:pg17` | `15432` | 全部结构化数据 + 向量 + 图谱 |

一个镜像服务多个容器：应用镜像里已包含 Python 运行时、MinerU 模型（约 4.6GB）、
Playwright 浏览器与全部依赖，因此**不需要外网**。

若需要外网访问网页，请在此服务器前面放反向代理并做好 TLS。

**端口暴露范围（上线前必须确认）**：

| 端口 | 暴露范围 | 说明 |
| --- | --- | --- |
| `8088`（主应用网页） | **所有网卡** | 主应用用 `network_mode: host`，且进程监听 `0.0.0.0`，因此**同网段任何人都能访问**。仅在可信内网使用，或用防火墙只放行反代所在地址；面向公网务必前置反向代理 + TLS |
| `18100` / `15432` / `19621` | 仅 `127.0.0.1` | 知识服务 API、PostgreSQL、LightRAG 都只绑定回环，外部不可达（推荐保持） |

只放行反代、封掉直连 8088 的例子（`<反代IP>` 换成实际地址）：

```bash
# 仅允许反向代理访问 8088，其余来源拒绝
iptables -A INPUT -p tcp --dport 8088 -s <反代IP> -j ACCEPT
iptables -A INPUT -p tcp --dport 8088 -j DROP
```

> `8088` 上的登录由 `.env` 的 `QWENPAW_AUTH_USERNAME` / `QWENPAW_AUTH_PASSWORD` 保护，
> 但**不要**因此把它直接暴露到公网。
