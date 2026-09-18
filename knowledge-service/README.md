# 汇智知识服务

## 数据库

知识服务运行时仅使用 PostgreSQL 17，不再依赖 SQLite。开发环境的默认 Compose 会启动
`knowledge-postgres`，并将数据库数据持久化在 `postgres-data/`；ARM64 正式环境建议使用
下文的独立 PostgreSQL Compose，数据库可以单独升级、备份和重启。

生产环境请在 .env 中配置现有 PG17 实例：

```text
KNOWLEDGE_DATABASE_URL=postgresql://用户名:密码@数据库主机:5432/huizhi_knowledge
```

从旧版 SQLite 迁移时，先备份 data/metadata.sqlite3，再在已构建的新知识服务镜像中运行：

```bash
python scripts/migrate_sqlite_to_postgres.py /data/metadata.sqlite3 \
  --database-url "$KNOWLEDGE_DATABASE_URL"
```

迁移工具会保留空间、文档、任务、分片、Embedding、实体、提及和关系数据；目标库非空时会拒绝覆盖。


这是汇智小助手的独立知识处理服务。它不依赖汇智小助手主进程，负责文档解析、异步处理、实体关系抽取、全文/向量检索和证据返回。

当前 `0.1.4` 提供两种镜像构建方式：核心镜像包含轻量解析、PostgreSQL 17 存储和检索能力；增强镜像额外安装 LightRAG 1.5.6、MinerU 3.4.5、PyTorch，并在构建阶段下载 `pipeline + VLM` 全量 MinerU 模型写入镜像。支持 PDF、DOC、DOCX、XLS、XLSX、XLSM、CSV、TXT、MD、JSON；DOC/DOCX 通过 LibreOffice 转 PDF 后进入 MinerU。生产服务器不需要联网，运行时从镜像内的本地模型目录解析。

## 运行结构

- `knowledge-api`：提供文件上传、任务状态、知识检索和证据接口。
- `knowledge-worker`：串行处理文档，避免大文件解析占满 API 进程。
- API 与 Worker 使用同一个知识服务镜像，原始文件使用 `/data` 数据卷持久化。
- `knowledge-postgres` 使用 PostgreSQL 17 持久化知识元数据、全文索引、Embedding、实体和关系。
- MinerU、LightRAG、Embedding 和 Reranker 都是可插拔能力；未安装增强依赖时，服务仍可用基础文本解析和 PostgreSQL 全文检索启动。

默认生产 Compose 为多用户服务器预留约 208GB 内存和 30 个 CPU 配额：主应用
48GB/8核，知识 API 16GB/4核，Worker 80GB/14核，LightRAG 32GB/4核，PostgreSQL
32GB/4核。`mem_limit`/`cpus` 是上限，不代表会常驻占用；宿主机仍需保留系统和 Docker
余量。可在 `.env` 中按实际并发和文件大小下调或上调各项 `*_MEMORY_LIMIT`、
`*_CPU_LIMIT`。

## 本地启动

```bash
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:18100/health
```

## ARM64 生产部署：基座镜像 + 代码挂载

服务器不需要 Docker build。将本目录（至少包含 `app/`、`scripts/`、
`docker-compose.code-mount.arm64.yml`、`.env` 和 `data/`）放到服务器，先导入本机
构建好的 `huizhi-knowledge:0.1.4-arm64` 镜像，再启动：

```bash
docker load -i huizhi-knowledge-0.1.4-arm64.tar
docker compose -f docker-compose.code-mount.arm64.yml up -d --no-build
```

构建基座时默认会下载 MinerU 的全部模型（需要在有互联网的本机执行）：

```bash
INSTALL_KNOWLEDGE_EXTRAS=1 PRELOAD_MINERU_MODELS=1 \
  bash scripts/docker_build_knowledge.sh huizhi-knowledge:0.1.4-arm64
```

不要把 `KNOWLEDGE_OFFLINE=true` 作为生产默认值；它会跳过 MinerU。生产配置应保持
`PARSER_BACKEND=auto`、`KNOWLEDGE_OFFLINE=false`、`MINERU_MODEL_SOURCE=local`，由
`auto` 优先调用镜像内 MinerU，只有模型损坏或单个任务异常时才回退到轻量解析器。

该 Compose 不启动 PostgreSQL 容器，`KNOWLEDGE_DATABASE_URL` 默认指向独立 PG Compose
映射的 `host.docker.internal:15432`。API 和 Worker 挂载 `./app`、`./scripts`，以后更新知识服务
代码只需解压代码更新包覆盖这两个目录，然后执行：

```bash
docker compose -f docker-compose.code-mount.arm64.yml up -d --no-build --force-recreate knowledge-api knowledge-worker
```

只有 Python/系统依赖、MinerU、LightRAG 或 PyTorch 发生变化时，才在本机重新构建并传输
新的基座镜像。

### 使用随附的 PostgreSQL 17 容器

如果不使用外部数据库，可以用单独的 Compose 文件启动 PostgreSQL 17：

```bash
docker compose --env-file .env -f docker-compose.postgres.yml up -d
```

然后使用代码挂载 Compose 启动 API、Worker 和 LightRAG。两个 Compose 项目可以独立维护和
独立重启：

```bash
docker compose --env-file .env -f docker-compose.postgres.yml up -d
docker compose --env-file .env -f docker-compose.code-mount.arm64.yml up -d --no-build
```

`docker-compose.postgres.yml` 只负责数据库；`docker-compose.code-mount.arm64.yml` 只负责
知识服务和代码挂载。不要启动第二份 PostgreSQL，也不要删除 `postgres-data/`。

```text
KNOWLEDGE_DATABASE_URL=postgresql://huizhi_knowledge:密码@host.docker.internal:15432/huizhi_knowledge
```

API/Worker Compose 已配置 `host.docker.internal` 到宿主机的映射。数据库数据持久化在
`postgres-data/`，不要删除该目录。

默认端口为 `18100`，只建议由汇智小助手通过 Docker 内部网络调用，不应直接暴露给普通用户。

## 模型接口

知识服务使用 OpenAI 兼容接口：

- 对话/抽取模型：`POST /v1/chat/completions`
- Embedding：`POST /v1/embeddings`
- Reranker：`POST /v1/rerank`，可选

模型不打进知识服务镜像。通过 `CHAT_BASE_URL`、`EMBEDDING_BASE_URL` 和 `RERANKER_BASE_URL` 连接内网模型服务。

上传前会实际验证 `EMBEDDING_*` 配置能否返回有效向量；地址、密钥或模型名称错误时上传按钮会被禁用，服务端也会拒绝新上传。视觉模型不参与上传前判断：Worker 会尝试解析图片，当前模型不支持多模态时自动跳过图片；文档文字和表格仍会入库。

## API

所有写入和查询接口支持 `X-Knowledge-Key`。如果未配置 `KNOWLEDGE_API_KEY`，仅适用于本机开发。

```text
GET  /health
POST /v1/spaces
GET  /v1/spaces
GET  /v1/spaces/{space_id}/documents
POST /v1/documents/upload
POST /v1/documents/parse              解析文档（返回纯文本，支持 PDF/DOCX/TXT/MD/CSV/XLSX/XLS/JSON）
GET  /v1/documents/{document_id}
GET  /v1/documents/{document_id}/stats  获取文档统计（含 started_at、error）
DELETE /v1/documents/{document_id}     删除文档（级联清理实体/关系/LightRAG）
GET  /v1/jobs/{job_id}
POST /v1/query
GET  /v1/evidence/{evidence_id}
POST /v1/search
POST /v1/documents/chunks
```

查询结果始终返回来源文件、页码/位置和原始片段，不能只返回模型总结。
