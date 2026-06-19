# Docker 离线交付说明

当前 Docker 交付链路拆成两层，并且已经按“外网准备、内网运行”的思路设计：

1. `deploy/Dockerfile.base`
   - 通用底座镜像
   - 适合在外网环境提前构建
   - 预装 Python 3.11、常用数据处理/接口开发依赖、Playwright Chromium、
     LibreOffice、OCR、Node.js/npm
   - 可安装 Oracle 11g 或 19c/21c/23c Instant Client，并自动设置环境变量
   - 预装 `oracledb`、`pyhive[hive_pure_sasl]`、`impyla` 等数据库 /
     大数据依赖
   - 提供 `cx_Oracle` 兼容导入层，减少旧脚本报错
   - 额外覆盖 SSH/自动化运维、对象存储、消息队列、配置管理、
     容器/K8s、主流数据库连接、常见数据格式处理等依赖
   - 支持多版本 Hadoop/Hive 客户端离线安装

2. `deploy/Dockerfile`
   - CoPaw 应用镜像
   - 基于底座镜像构建，只负责放入最新官方主干代码、安装 CoPaw
     自身依赖并生成最终运行镜像
   - 默认直接使用官方打包好的 console 静态资源，不再依赖构建期联网
     下载前端依赖

## 官方代码来源说明

当前这条内网版主线，已切换到最新官方 Python 主干：

- 官方最新稳定包：`qwenpaw 1.1.9`
- 获取方式：官方 PyPI 包源
- 兼容策略：
  - 保留 `copaw` 命令入口
  - 保留 `COPAW_*` 环境变量兼容
  - 在仓库中同时提供 `src/qwenpaw` 主代码与 `src/copaw` 兼容链接

## 推荐流程

在有网机器上：

```bash
# 面向最终服务器交付时，优先准备 amd64/x86_64 资源
bash scripts/fetch_offline_clients.sh amd64

# 1) 下载 Hadoop/Hive 离线包（按架构）
# 默认下载: Hadoop 3.0.1, Hive 2.3.9, Hive 3.1.3
bash scripts/fetch_offline_clients.sh amd64
# 本机 ARM 联调用
bash scripts/fetch_offline_clients.sh arm64

# 自定义版本下载:
HADOOP_VERSION=3.3.6 HIVE2_VERSION=2.3.9 HIVE3_VERSION=3.1.3 bash scripts/fetch_offline_clients.sh amd64

# 2) 手动准备 Oracle 包
# deploy/offline-assets/arm64/oracle/
# deploy/offline-assets/amd64/oracle/
# amd64: 可提供 Oracle 11g 或 19c/21c/23c basic zip
# arm64: 可不提供 Oracle（默认跳过安装）

# 3) 构建底座镜像（服务器交付优先用 amd64）
PLATFORM=linux/amd64 bash scripts/docker_build_base.sh py311-base:1.0.0-amd64
# 本机 ARM 联调用
PLATFORM=linux/arm64 bash scripts/docker_build_base.sh py311-base:1.0.0-arm64

# 4) 构建 CoPaw 应用镜像（服务器交付优先用 amd64）
PLATFORM=linux/amd64 BASE_IMAGE=py311-base:1.0.0-amd64 bash scripts/docker_build.sh mycopaw-offline:2.0.0-amd64
# 本机 ARM 联调用
PLATFORM=linux/arm64 BASE_IMAGE=py311-base:1.0.0-arm64 bash scripts/docker_build.sh mycopaw-offline:2.0.0-arm64

# 也可以直接用一键 x86 交付脚本
bash scripts/docker_build_x86_release.sh 2.0.0

# 5) 导出镜像
docker save -o dist/docker-images/py311-base-1.0.0-amd64.tar py311-base:1.0.0-amd64
docker save -o dist/docker-images/mycopaw-offline-2.0.0-amd64.tar mycopaw-offline:2.0.0-amd64
docker save -o dist/docker-images/py311-base-1.0.0-arm64.tar py311-base:1.0.0-arm64
docker save -o dist/docker-images/mycopaw-offline-2.0.0-arm64.tar mycopaw-offline:2.0.0-arm64
```

在内网机器上：

```bash
docker load -i dist/docker-images/py311-base-1.0.0-amd64.tar
docker load -i dist/docker-images/mycopaw-offline-2.0.0-amd64.tar
docker run -d --name mycopaw -p 8088:8088 mycopaw-offline:2.0.0-amd64
```

## 离线资源目录

可选离线客户端包统一放在 `deploy/offline-assets/<arch>/`：

```
deploy/offline-assets/
├── amd64/
│   ├── hadoop/          # Hadoop 客户端包
│   ├── hive2/          # Hive 2.x 客户端包（兼容 Hadoop 2.x-3.x）
│   ├── hive3/          # Hive 3.x 客户端包（需要 Hadoop >= 3.1.0）
│   └── oracle/         # Oracle Instant Client 包
└── arm64/
    ├── hadoop/
    ├── hive2/
    ├── hive3/
    └── oracle/
```

### 版本兼容性

| 组件 | 推荐版本 | Hadoop 兼容性 | 说明 |
|------|----------|---------------|------|
| Hadoop | 3.0.1 / 3.3.6 | - | 3.0.1 可对齐旧生产环境，3.3.6 适合新环境 |
| Hive 2.x | 2.3.9 | Hadoop 2.x, 3.x | 广泛兼容，适合混合环境 |
| Hive 3.x | 3.1.3 | Hadoop >= 3.1.0 | 新环境推荐，Hadoop 3.1+ |

### 环境变量

- `HADOOP_HOME=/opt/hadoop` - 当前使用的 Hadoop
- `HIVE_HOME=/opt/hive` - 当前使用的 Hive（默认指向 Hive 3.x）
- `HIVE2_HOME=/opt/hive2` - Hive 2.x 安装位置
- `HIVE3_HOME=/opt/hive3` - Hive 3.x 安装位置
- `PATH` 包含所有 bin 目录，可直接调用 `beeline`、`hive`、`hdfs` 等命令

### 切换 Hive 版本

运行时可通过环境变量切换：

```bash
# 使用 Hive 2.x
docker run -e HIVE_HOME=/opt/hive2 -e PATH="/opt/hadoop/bin:/opt/hive2/bin:..." ...

# 使用 Hive 3.x（默认）
docker run -e HIVE_HOME=/opt/hive3 -e PATH="/opt/hadoop/bin:/opt/hive3/bin:..." ...
```

## 说明

- 最终部署服务器是 `x86_64`，因此正式交付镜像应以 `linux/amd64` 构建结果为准。
- `arm64` 镜像只建议用于本机开发机或 Apple Silicon 环境联调，不作为最终服务器镜像。

- Hadoop/Hive 安装包如果缺失，底座镜像会跳过安装并输出提示。
- amd64 下 Oracle 可提供 11g 或 19c/21c/23c basic zip；如果没有提供，则会跳过 Oracle 安装。
- arm64 下 Oracle 可不提供，底座镜像会跳过 Oracle 安装。
- Hive 2.x 和 Hive 3.x 可同时安装，通过环境变量切换使用哪个。
- Oracle basic zip 由你们在外网环境手动准备后放入对应架构目录，
  构建过程不会主动联网下载。
- 底座镜像预装了更完整的 Python 常用依赖，覆盖数据分析、Web/API、
  文档处理、数据库连接、大数据访问等场景。
- 已额外纳入一批运维常用包，例如 `paramiko`、`fabric`、`sshtunnel`、
  `ansible-core`、`docker`、`kubernetes`、`netmiko`、`ncclient`、
  `scrapli`、`hvac`、`celery`、`kafka-python`、`elasticsearch`、
  `opensearch-py`、`boto3`、`minio`、`python-dotenv`、`tenacity` 等。
- 已额外纳入一批主流数据库与数仓驱动，例如 `trino`、
  `clickhouse-connect`、`clickhouse-driver`、`neo4j`、
  `cassandra-driver`、`influxdb-client`、`pyathena`、
  `vertica-python`、`snowflake-connector-python` 等。
- 已额外纳入常见数据文件与数据工程依赖，例如 `dask`、`fsspec`、
  `s3fs`、`fastparquet`、`pyxlsb`、`pyreadstat`、`xmltodict`、
  `ruamel.yaml`、`sqlmodel` 等。
- Oracle 相关 Python 驱动默认提供 `oracledb`，同时兼容 `import cx_Oracle`
  的旧代码写法。
- Hive 相关 Python 驱动使用 `pyhive[hive_pure_sasl]`，避免 `pyhive[hive]`
  在 Python 3.11 下依赖 `sasl` 导致构建或运行失败。
- Hadoop/Hive 相关安装包建议在外网环境提前准备，配置文件不写死进镜像，
  运行时通过 `docker compose` 挂载即可。
- 应用镜像默认复用官方包里已经打好的 console 静态资源，这样更适合
  内网交付，减少额外前端依赖下载。
- 这种模式适合"外网构建、内网运行"。
- 如果后续别的内部服务也需要 Python 数据栈、浏览器、OCR、Hadoop/Hive
  客户端，可以直接复用底座镜像，避免重复下载。
