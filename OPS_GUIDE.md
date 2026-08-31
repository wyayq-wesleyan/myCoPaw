# 运维指南 — 镜像管理与升级策略

> 核心原则：**不要重新构建镜像**。所有变更直接在运行中的容器内完成，然后 `docker commit` 保存为新镜像。

## 1. 当前镜像清单

| 镜像 | 标签 | 大小 | 用途 | 基座 |
|------|------|------|------|------|
| `huizhi-assistant` | `2.1.0-base` | ~19GB | 汇智小助手主应用 | `huizhi-assistant:2.1.0-arm64` |
| `huizhi-knowledge` | `0.1.4-base` | ~23GB | 知识服务（API + Worker + LightRAG） | `huizhi-knowledge:0.1.4-arm64` |

> **基座镜像**（`2.1.0-arm64`、`0.1.4-arm64`）是最初从 Dockerfile 构建的，仅用于 emergency reset。日常升级一律在 `-base` 镜像上操作。

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

### 2.2 保存为新镜像

```bash
# 汇智小助手
docker commit huizhi-v210-check huizhi-assistant:2.1.0-base

# 知识服务（任一容器均可，代码共享）
docker commit huizhi-knowledge-worker huizhi-knowledge:0.1.4-base
```

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
    image: "huizhi-knowledge:0.1.4-base"   # 改为新标签
```

## 3. 汇智小助手（huizhi-v210-check）

### 3.1 容器信息

| 项目 | 值 |
|------|-----|
| 容器名 | `huizhi-v210-check` |
| 镜像 | `huizhi-assistant:2.1.0-base` |
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
```bash
docker exec huizhi-v210-check pip install <package>
docker commit huizhi-v210-check huizhi-assistant:2.1.0-base
```

**查看日志：**
```bash
docker logs huizhi-v210-check --tail 100
```

## 4. 知识服务

### 4.1 容器信息

| 容器 | 镜像 | 端口 | 用途 |
|------|------|------|------|
| `huizhi-knowledge-api` | `huizhi-knowledge:0.1.4-base` | `18100` | REST API |
| `huizhi-knowledge-worker` | `huizhi-knowledge:0.1.4-base` | — | 异步文档处理 |
| `huizhi-lightrag` | `huizhi-knowledge:0.1.4-base` | `19621` | 知识图谱 |
| `huizhi-knowledge-postgres` | `postgres:17-alpine` | `15432` | 数据库 |

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
```bash
docker exec huizhi-knowledge-worker pip install <package>
docker commit huizhi-knowledge-worker huizhi-knowledge:0.1.4-base
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
# 用 docker-compose 或手动启动命令重新创建容器，镜像改为 huizhi-assistant:2.1.0-arm64

# 重置知识服务（回到原始构建镜像）
docker stop huizhi-knowledge-worker huizhi-knowledge-api
docker rm huizhi-knowledge-worker huizhi-knowledge-api
# 用 docker-compose 重新创建，镜像改为 huizhi-knowledge:0.1.4-arm64
```

## 6. 备份

```bash
# 导出镜像
docker save huizhi-assistant:2.1.0-base | gzip > huizhi-assistant-2.1.0-base.tar.gz
docker save huizhi-knowledge:0.1.4-base | gzip > huizhi-knowledge-0.1.4-base.tar.gz

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

# 2. 提交为新镜像
docker commit <container> <image>:<tag>

# 3. 更新本文档的「已安装的关键依赖」表格
```
