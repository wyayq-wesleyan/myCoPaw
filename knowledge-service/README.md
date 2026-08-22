# 汇智知识服务

## 数据库

知识服务运行时仅使用 PostgreSQL 17，不再依赖 SQLite。默认 Compose 会启动 knowledge-postgres，并将数据库数据持久化在 postgres-data/。

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

当前 `0.1.4` 提供两种镜像构建方式：核心镜像包含轻量解析、PostgreSQL 17 存储和检索能力；增强镜像额外安装 LightRAG 1.5.6 与 MinerU 3.4.5。MinerU 的本地 pipeline 还需要 PyTorch 和本地模型文件，未准备这些文件时服务会自动回退到轻量解析；扫描 PDF 的 OCR 会显示明确的依赖错误，不会伪装成处理成功。

## 运行结构

- `knowledge-api`：提供文件上传、任务状态、知识检索和证据接口。
- `knowledge-worker`：串行处理文档，避免大文件解析占满 API 进程。
- API 与 Worker 使用同一个知识服务镜像，原始文件使用 `/data` 数据卷持久化。
- `knowledge-postgres` 使用 PostgreSQL 17 持久化知识元数据、全文索引、Embedding、实体和关系。
- MinerU、LightRAG、Embedding 和 Reranker 都是可插拔能力；未安装增强依赖时，服务仍可用基础文本解析和 PostgreSQL 全文检索启动。

## 本地启动

```bash
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:18100/health
```

默认端口为 `18100`，只建议由汇智小助手通过 Docker 内部网络调用，不应直接暴露给普通用户。

## 模型接口

知识服务使用 OpenAI 兼容接口：

- 对话/抽取模型：`POST /v1/chat/completions`
- Embedding：`POST /v1/embeddings`
- Reranker：`POST /v1/rerank`，可选

模型不打进知识服务镜像。通过 `CHAT_BASE_URL`、`EMBEDDING_BASE_URL` 和 `RERANKER_BASE_URL` 连接内网模型服务。

## API

所有写入和查询接口支持 `X-Knowledge-Key`。如果未配置 `KNOWLEDGE_API_KEY`，仅适用于本机开发。

```text
GET  /health
POST /v1/spaces
GET  /v1/spaces
GET  /v1/spaces/{space_id}/documents
POST /v1/documents/upload
GET  /v1/documents/{document_id}
GET  /v1/jobs/{job_id}
POST /v1/query
GET  /v1/evidence/{evidence_id}
```

查询结果始终返回来源文件、页码/位置和原始片段，不能只返回模型总结。
