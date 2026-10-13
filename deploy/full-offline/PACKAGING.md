# 全量离线部署包 —— 维护者说明

这里存放**全量离线部署包**（装机包）的源文件。该包面向「一台全新的 x86_64 服务器、
挂在 `/data`、无外网」的场景：含镜像、代码、前端产物、部署文件与 `.env` 模板。

面向客户的使用说明在 `README.md`（会被打进包里）。本文是给出包人的内部说明。

---

## 一、出包

```bash
cd <仓库根>
bash deploy/full-offline/scripts/package_full_offline.sh
```

产物（脚本结束时会打印绝对路径）：

```
dist/full-offline/huizhi-assistant-full-YYYYMMDD.tar.gz          # 完整包（约 6.6GB）
dist/full-offline/huizhi-assistant-full-YYYYMMDD.tar.gz.sha256
dist/full-offline/parts/                                         # 可直接传输的分卷
├── huizhi-assistant-full-YYYYMMDD.tar.gz.part-00 … part-NN      # 每卷 900MiB
├── huizhi-assistant-full-YYYYMMDD.tar.gz.sha256
└── join-parts.sh                                                # 目标机器上合并用
```

**为什么有 `parts/`**：完整包约 6.6GB，超过很多传输通道/网盘 1GB 的单文件上限。
脚本默认按 900MiB 切分（`split -b 900m -a 2 -d`），实际交付的是 `parts/` 目录；
目标机器上跑 `bash join-parts.sh` 合并回完整包并自动核对 sha256。
脚本在出包时会**当场验证一次「分卷合并后的 sha256 == 原包 sha256」**，不合就中止出包，
所以不会把坏分卷发出去。

调整分卷大小或关掉分卷：

```bash
HUIZHI_PART_SIZE=500m bash deploy/full-offline/scripts/package_full_offline.sh   # 更小的卷
HUIZHI_PART_SIZE=none bash deploy/full-offline/scripts/package_full_offline.sh   # 不分卷
```

> `split` 的 `-b 900m` 在 GNU coreutils 与 BSD 上含义一致（均为 900×1024×1024 字节，
> 已实测）；两种平台上生成的分卷可互相合并，因为它们只是原始文件的字节切片。

同名包同一天重复打包会覆盖，需要留档时把自定义文件名作为脚本第一个参数：

```bash
bash deploy/full-offline/scripts/package_full_offline.sh huizhi-assistant-full-20260928-v2.tar.gz
```

### 前置条件

1. **amd64 底座镜像已构建**：默认 `huizhi-runtime:2.2.1-amd64`。
   仓库里同时存在 arm64 镜像，脚本会校验架构，拿错架构直接报错退出。
2. **前端已构建**：`console/dist/index.html` 必须存在。
   脚本**不替你构建**——请先 `cd console && npm run build`（内部是 `tsc -b`，见 AGENTS.md）。
3. **pgvector 镜像已就位且为 amd64**：默认 `pgvector/pgvector:pg17`。
   ⚠️ 多架构镜像在本机 `docker pull` 默认取**宿主架构**。在 Apple Silicon 上直接
   `docker pull pgvector/pgvector:pg17` 得到的是 arm64，脚本会拒绝。
   正确做法是按 manifest 摘要拉 amd64 变体再打上规范标签：

   ```bash
   # 1) 找到 amd64 变体的 digest
   docker buildx imagetools inspect docker.1ms.run/pgvector/pgvector:pg17
   # 2) 按 digest 拉取（--platform 在本机可能不生效，用 digest 最稳）
   docker pull --platform linux/amd64 \
     docker.1ms.run/pgvector/pgvector@sha256:<amd64 的 digest>
   # 3) 用 amd64 变体占用规范标签（compose 引用的就是它）
   docker tag <amd64 镜像ID> pgvector/pgvector:pg17
   docker image inspect --format '{{.Architecture}}' pgvector/pgvector:pg17   # 必须是 amd64
   ```

   出包后记得把本机标签还原成宿主架构，免得影响本地开发：

   ```bash
   docker tag <arm64 镜像ID> pgvector/pgvector:pg17
   ```

### 镜像归档复用

`dist/full-offline/` 下已存在的 `huizhi-runtime-<版本>-<架构>.tar.zst` 与
`pgvector-pg17-<架构>.tar.zst` 会被**直接复用**（底座镜像导出一次要很久）。
要强制重新导出，先删掉对应文件。

---

## 二、版本号与镜像名

脚本用环境变量控制，默认值：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HUIZHI_VERSION` | `2.2.1` | 出现在镜像 tag 与归档文件名里 |
| `HUIZHI_ARCH` | `amd64` | 目标架构 |
| `RUNTIME_IMAGE` | `huizhi-runtime:${HUIZHI_VERSION}-${HUIZHI_ARCH}` | 底座镜像 |
| `PGVECTOR_IMAGE` | `pgvector/pgvector:pg17` | 数据库镜像 |

只有底座依赖（PyTorch / MinerU / LightRAG / Dockerfile）变化时才需要重建底座镜像并
换 `HUIZHI_VERSION`。纯代码改动**不需要**重出全量包——那种情况走日常更新包
（`scripts/package_merge_updates.sh`），全量包只用于全新装机。

---

## 三、脚本做了什么

1. 组装 `dist/full-offline/bundle/huizhi-assistant/`
   - `deploy/full-offline/README.md` → 包内 `README.md`（客户文档）
   - `deploy/full-offline/env/*.example` → 包内 `.env` 与 `.env.example`（两份相同，客户改 `.env`）
   - `deploy/full-offline/scripts/*.sh` → 包内 `scripts/`
   - `deploy/docker-compose.code-mount.<arch>.yml` → 包内 `docker-compose.yml`
   - `knowledge-service/docker-compose.code-mount.<arch>.yml` → 包内 `knowledge-service/docker-compose.yml`
   - `knowledge-service/docker-compose.postgres.yml`、`initdb/`、entrypoint、supervisord 模板
   - `src/`、`console/dist/`、`knowledge-service/app/`（排除 `__pycache__`）
   - 两个镜像归档 + sha256
2. 结构断言（**防回归**）：直接挡住几个曾经真实踩过的坑
   - `knowledge-service/app/main.py` 必须存在（`cp` 到已存在目录会多套一层 `app/app`）
   - 包内不得混入 `__pycache__`
   - 三个 compose 必须能被 `docker compose config` 解析
3. 打 `tar.gz` 并生成 `.sha256` 与 `.absolute-paths.txt`
4. 切分卷到 `parts/`，并把 `join-parts.sh` 与 `.sha256` 一并放进去；
   当场验证「合并后 sha256 == 原包 sha256」，不一致直接中止出包

---

## 四、相关文件

- 包内客户文档：`README.md`
- 主应用 compose 源：`deploy/docker-compose.code-mount.amd64.yml`
- 知识服务 compose 源：`knowledge-service/docker-compose.code-mount.amd64.yml`
- PostgreSQL compose 源：`knowledge-service/docker-compose.postgres.yml`
- 图谱库初始化 SQL：`deploy/full-offline/config/01-create-graph-database.sql`
- 镜像导出脚本：`scripts/docker_export_image.sh`
