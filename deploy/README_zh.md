# Docker 离线交付说明

当前 Docker 交付链路拆成两层：

1. `deploy/Dockerfile.base`
   - 通用底座镜像
   - 适合在外网环境提前构建
   - 预装 Python 3.11、常用数据处理/接口开发依赖、Playwright Chromium、
     LibreOffice、OCR、Node.js/npm
   - 预装 Oracle 11g Instant Client 并自动设置环境变量
   - 预装 `oracledb`、`pyhive[hive_pure_sasl]`、`impyla` 等数据库 /
     大数据依赖
   - 提供 `cx_Oracle` 兼容导入层，减少旧脚本报错
   - 预留 Hadoop 3、Hive 3 客户端的离线安装位
2. `deploy/Dockerfile`
   - CoPaw 应用镜像
   - 基于底座镜像构建，只负责放入 CoPaw 源码、构建 console、安装
     CoPaw 自身依赖并生成最终运行镜像

## 推荐流程

在有网机器上：

```bash
# 1) 下载 Hadoop/Hive 离线包（按架构）
bash scripts/fetch_offline_clients.sh arm64
# 或
bash scripts/fetch_offline_clients.sh amd64

# 2) 手动准备 Oracle 包
# deploy/offline-assets/arm64/oracle/
# deploy/offline-assets/amd64/oracle/
# amd64: Oracle 11g basic zip 必须提供
# arm64: 可不提供 Oracle（默认跳过安装）

# 3) 构建底座镜像
PLATFORM=linux/arm64 bash scripts/docker_build_base.sh py311-base:1.0.0-arm64
# 或
PLATFORM=linux/amd64 bash scripts/docker_build_base.sh py311-base:1.0.0-amd64

# 4) 构建 CoPaw 应用镜像
PLATFORM=linux/arm64 BASE_IMAGE=py311-base:1.0.0-arm64 bash scripts/docker_build.sh mycopaw-offline:2.0.0-arm64
# 或
PLATFORM=linux/amd64 BASE_IMAGE=py311-base:1.0.0-amd64 bash scripts/docker_build.sh mycopaw-offline:2.0.0-amd64

# 5) 导出镜像
docker save -o dist/docker-images/py311-base-1.0.0-arm64.tar py311-base:1.0.0-arm64
docker save -o dist/docker-images/mycopaw-offline-2.0.0-arm64.tar mycopaw-offline:2.0.0-arm64
```

在内网机器上：

```bash
docker load -i dist/docker-images/py311-base-1.0.0.tar
docker load -i dist/docker-images/mycopaw-offline-2.0.0.tar
docker run -d --name mycopaw -p 8088:8088 mycopaw-offline:2.0.0
```

## 离线资源目录

可选离线客户端包统一放在 `deploy/offline-assets/`：

- `deploy/offline-assets/arm64/hadoop/`
- `deploy/offline-assets/arm64/hive/`
- `deploy/offline-assets/arm64/oracle/`
- `deploy/offline-assets/amd64/hadoop/`
- `deploy/offline-assets/amd64/hive/`
- `deploy/offline-assets/amd64/oracle/`

- Hadoop/Hive 安装包如果缺失，底座镜像会跳过安装。
- amd64 下 Oracle 11g basic zip 如果缺失，底座镜像会直接失败。
- arm64 下 Oracle 可不提供，底座镜像会跳过 Oracle 安装。

## 说明

- `amd64` 使用 Oracle 11g Instant Client（建议与目标服务器一致的 11g 版本）。
- `arm64` 本地测试可不带 Oracle。
- Oracle basic zip 由你们在外网环境手动准备后放入对应架构目录，
  构建过程不会主动联网下载。
- 底座镜像预装了更完整的 Python 常用依赖，覆盖数据分析、Web/API、
  文档处理、数据库连接、大数据访问等场景。
- Oracle 相关 Python 驱动默认提供 `oracledb`，同时兼容 `import cx_Oracle`
  的旧代码写法。
- Hive 相关 Python 驱动使用 `pyhive[hive_pure_sasl]`，避免 `pyhive[hive]`
  在 Python 3.11 下依赖 `sasl` 导致构建或运行失败。
- Hadoop/Hive 相关安装包建议在外网环境提前准备，配置文件不写死进镜像，
  运行时通过 `docker compose` 挂载即可。
- 这种模式适合“外网构建、内网运行”。
- 如果后续别的内部服务也需要 Python 数据栈、浏览器、OCR、Hadoop/Hive
  客户端，可以直接复用底座镜像，避免重复下载。
