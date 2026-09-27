# 运维指南 — 镜像管理与升级策略

> 核心原则：**代码变更走代码挂载，不要重建镜像**。镜像只承载依赖；日常更新替换
> 挂载目录中的代码并重启容器即可（见 README 打包约定）。只有依赖、MinerU、
> LightRAG、PyTorch 或 Dockerfile 变化时才重建。

## 1. 当前镜像清单

**全部服务共用一个镜像**，各自用不同命令启动：

| 服务 | 容器 | 启动命令 | 端口 |
|------|------|----------|------|
| 汇智小助手 | `huizhi-assistant` | `/entrypoint.sh`（Supervisor） | `8088` |
| 知识服务 API | `huizhi-knowledge-api` | `uvicorn app.main:app` | `18100` |
| 知识服务 Worker | `huizhi-knowledge-worker` | `python -m app.worker` | — |
| LightRAG | `huizhi-lightrag` | `python -m lightrag.api.lightrag_server` | `9621` |

| 镜像 | 大小 | 说明 |
|------|------|------|
| `huizhi-runtime:2.2.1-arm64` / `-amd64` | 约 20GB（导出 6.8GB） | 依赖超集，内置 MinerU 模型（4.6GB）与 CPU 版 torch |
| `py312-base:2.0.2-arm64` / `-amd64` | 约 7.8GB | 系统依赖底座；与全量镜像共享层，仅用于重建 |

> **为什么合并成一个镜像**：小助手与知识服务的依赖层高度重叠。分成两个镜像会让
> 这些层在导出包里各存一份，白白多占服务器与本机空间。合并后两服务共享依赖层，
> 导出包只多出一份代码体积。

> **为什么用 CPU 版 torch**：默认 PyPI 的 torch 会拖入约 4.4GB 的 CUDA 运行时，
> 而无 GPU 部署完全用不到。直接卸载这些包会让 torch 崩溃（`libcublasLt` 是启动时
> 加载的），因此改为从阿里云镜像安装 `+cpu` 版本。

## 2. 升级流程（两个服务通用）

### 2.1 在容器内安装依赖 / 修改代码

```bash
# 进入容器
docker exec -it huizhi-v210-check bash        # 汇智小助手
docker exec -it huizhi-knowledge-worker bash   # 知识服务 Worker
docker exec -it huizhi-knowledge-api bash      # 知识服务 API

# 安装 Python 包
pip install <package> --index-url https://mirrors.aliyun.com/pypi/simple

# 安装系统包
apt-get update && apt-get install -y <package>

# 修改应用代码（容器内路径）
vi /workspace/app/src/qwenpaw/xxx.py           # 汇智小助手
vi /opt/huizhi-knowledge/app/xxx.py            # 知识服务
```

### 2.2 不推荐 docker commit

`docker commit` 曾用于把容器内的改动固化成镜像，但**合并成单镜像后不要再用**：
所有服务共享同一个镜像，从一个容器 commit 会连带覆盖其他服务的应用代码，
很容易把某个服务的旧代码写进共享镜像。

依赖变化请改用脚本重建（会带上正确的源与 MinerU 预置）：

```bash
# 本机（arm64）重建
bash scripts/docker_build_allinone.sh huizhi-runtime:2.2.1-arm64
# 生产（amd64；跨架构构建很慢，优先在同架构机器上执行）
PLATFORM=linux/amd64 bash scripts/docker_build_allinone.sh huizhi-runtime:2.2.1-amd64

# 导出给内网服务器
bash scripts/docker_export_image.sh huizhi-runtime:2.2.1-amd64
```

只改应用代码时不需要动镜像：替换挂载目录里的 `app/` 与 `knowledge-service/app/`
再重启容器即可。

### 2.3 重启容器验证

```bash
docker restart huizhi-v210-check
docker restart huizhi-knowledge-worker
docker restart huizhi-knowledge-api
```

### 2.4 更新 docker-compose.yml 中的镜像标签（可选）

如果需要让 `docker-compose up` 使用新镜像，修改对应 image 字段：

```yaml
# docker-compose.yml
services:
  knowledge-worker:
    image: "huizhi-runtime:2.2.1-arm64"   # 改为新标签
```

## 3. 汇智小助手（huizhi-v210-check）

### 3.1 容器信息

| 项目 | 值 |
|------|-----|
| 容器名 | `huizhi-v210-check` |
| 镜像 | `huizhi-runtime:2.2.1-arm64` |
| 端口 | `18088` |
| 代码路径（容器内） | `/workspace/app/` |
| 前端静态文件 | `/workspace/app/console/dist/` |
| 模型服务 | `http://host.docker.internal:9988/v1` |
| API Key | `thinker` |

### 3.2 常见操作

**更新前端：**
```bash
# 本地构建
cd console && npm run build

# 部署到容器
docker cp console/dist/. huizhi-v210-check:/workspace/app/console/dist/
docker restart huizhi-v210-check
```

**更新后端代码：**
```bash
# 复制修改的文件到容器
docker cp src/qwenpaw/xxx.py huizhi-v210-check:/workspace/app/src/qwenpaw/xxx.py
docker restart huizhi-v210-check
```

**安装 Python 依赖：**

依赖必须固化进镜像（容器内直接 pip 安装会在容器重建时丢失），因此改用重建脚本，
不要在容器里 `docker commit`——所有服务共享该镜像，commit 会覆盖其他服务的代码：
```bash
bash scripts/docker_build_allinone.sh huizhi-runtime:2.2.1-arm64
```

**查看日志：**
```bash
docker logs huizhi-v210-check --tail 100
```

## 4. 知识服务

### 4.1 容器信息

| 容器 | 镜像 | 端口 | 用途 |
|------|------|------|------|
| `huizhi-knowledge-api` | `huizhi-runtime:2.2.1-arm64` | `18100` | REST API |
| `huizhi-knowledge-worker` | `huizhi-runtime:2.2.1-arm64` | — | 异步文档处理 |
| `huizhi-lightrag` | `huizhi-runtime:2.2.1-arm64` | `19621` | 知识图谱 |
| `huizhi-knowledge-postgres` | `pgvector/pgvector:pg17` | `15432` | 数据库 |

### 4.2 容器内路径

| 路径 | 说明 |
|------|------|
| `/opt/huizhi-knowledge/app/` | 应用代码（API + Worker 共享） |
| `/data/` | 文档存储（volume 挂载） |
| `/data/lightrag/` | LightRAG 数据 |

### 4.3 数据库信息

| 项目 | 值 |
|------|-----|
| 数据库 | `huizhi_knowledge` |
| 用户 | `huizhi_knowledge` |
| 密码 | `huizhi_knowledge` |
| 连接 | `host.docker.internal:15432` |

### 4.4 已安装的关键依赖

| 包 | 版本 | 说明 |
|----|------|------|
| `mineru` | 3.4.5 | PDF/DOCX 文档解析（MinerU） |
| `torch` | 2.13.0+cpu | MinerU 运行时依赖 |
| `lightrag-hku` | 1.5.6 | 知识图谱 |
| `python-docx` | 1.2.0 | DOCX 备用解析 |
| `pdfplumber` | 0.11.10 | PDF 备用解析 |

### 4.5 常见操作

**重新处理失败的文档：**
```bash
docker exec huizhi-knowledge-worker python3 -c "
import psycopg2
conn = psycopg2.connect('postgresql://huizhi_knowledge:huizhi_knowledge@host.docker.internal:15432/huizhi_knowledge')
cur = conn.cursor()
cur.execute(\"UPDATE documents SET status = 'pending', error = NULL WHERE id = '<DOCUMENT_ID>'\")
cur.execute(\"UPDATE jobs SET status = 'queued', attempts = 0, error = NULL WHERE document_id = '<DOCUMENT_ID>'\")
conn.commit()
conn.close()
"
```

**查看文档状态：**
```bash
docker exec huizhi-knowledge-worker python3 -c "
import psycopg2
conn = psycopg2.connect('postgresql://huizhi_knowledge:huizhi_knowledge@host.docker.internal:15432/huizhi_knowledge')
cur = conn.cursor()
cur.execute('SELECT id, filename, status, backend, error FROM documents ORDER BY created_at DESC LIMIT 10')
for row in cur.fetchall(): print(row)
conn.close()
"
```

**安装新依赖：**

同样需要重建镜像（见 2.2），不要 `docker commit`：
```bash
bash scripts/docker_build_allinone.sh huizhi-runtime:2.2.1-arm64
```

**查看 Worker 日志：**
```bash
docker logs huizhi-knowledge-worker --tail 100
```

## 5. 紧急重置

⚠️ **仅在容器损坏无法修复时使用，会丢失容器内所有变更。**

```bash
# 重置汇智小助手（回到原始构建镜像）
docker stop huizhi-v210-check
docker rm huizhi-v210-check
# 用 docker-compose 或手动启动命令重新创建容器，镜像改为 huizhi-runtime:2.2.1-arm64

# 重置知识服务（回到原始构建镜像）
docker stop huizhi-knowledge-worker huizhi-knowledge-api
docker rm huizhi-knowledge-worker huizhi-knowledge-api
# 用 docker-compose 重新创建，镜像改为 huizhi-runtime:2.2.1-arm64
```

## 6. 备份

```bash
# 导出镜像
docker save huizhi-runtime:2.2.1-arm64 | gzip > huizhi-assistant-2.1.0-base.tar.gz
docker save huizhi-runtime:2.2.1-arm64 | gzip > huizhi-knowledge-0.1.4-base.tar.gz

# 导出数据库
docker exec huizhi-knowledge-postgres pg_dump -U huizhi_knowledge huizhi_knowledge > huizhi_knowledge_backup.sql

# 导出文档数据
docker cp huizhi-knowledge-api:/data/ ./knowledge-data-backup/
```

## 7. 新增依赖检查清单

在容器内安装新包后，务必执行：

```bash
# 1. 验证包可用
docker exec <container> python3 -c "import <package>; print('ok')"

# 2. 重建镜像（不要 docker commit，见 2.2）
bash scripts/docker_build_allinone.sh huizhi-runtime:2.2.1-arm64

# 3. 更新本文档的「已安装的关键依赖」表格
```
