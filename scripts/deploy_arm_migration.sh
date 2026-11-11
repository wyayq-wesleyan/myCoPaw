#!/usr/bin/env bash
# Run on the NEW ARM server from the extracted migration package.
# Deploys code/config only; data directories start empty.
#
# Target:
#   /data/huizhi-assistant
#   /data/knowledge-service
#   /postgres-data
set -euo pipefail

PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-/data/huizhi-assistant}"
KNOWLEDGE_DIR="${KNOWLEDGE_DIR:-/data/knowledge-service}"
PG_DATA="${PG_DATA:-/postgres-data}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/data/huizhi-migration-backup-${STAMP}"
SKIP_IMAGE=0
REUSE_DATA=0
ASSUME_YES=0
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-300}"

die() { echo "错误：$*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP_DIR="${2:-}"; shift 2 ;;
    --knowledge) KNOWLEDGE_DIR="${2:-}"; shift 2 ;;
    --pg-data) PG_DATA="${2:-}"; shift 2 ;;
    --skip-image) SKIP_IMAGE=1; shift ;;
    --reuse-data) REUSE_DATA=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "请用 root 执行"
[ "$(uname -m)" = "aarch64" ] || die "当前架构不是 aarch64/arm64"
docker compose version >/dev/null 2>&1 || die "docker compose 不可用"

for f in "${PKG_ROOT}/app/src" "${PKG_ROOT}/app-config/docker-compose.yml" \
         "${PKG_ROOT}/app-config/.env" "${PKG_ROOT}/knowledge-service/app" \
         "${PKG_ROOT}/knowledge-config/docker-compose.yml" \
         "${PKG_ROOT}/knowledge-config/.env" \
         "${PKG_ROOT}/knowledge-config/docker-compose-pg.yml"; do
  [ -e "${f}" ] || die "迁移包不完整：缺少 ${f}"
done

APP_COMPOSE="${APP_DIR}/docker-compose.yml"
KNOW_COMPOSE="${KNOWLEDGE_DIR}/docker-compose.yml"
PG_COMPOSE="${KNOWLEDGE_DIR}/docker-compose-pg.yml"

echo "主应用目录：  ${APP_DIR}"
echo "知识服务目录：${KNOWLEDGE_DIR}"
echo "PG 数据目录： ${PG_DATA}"
echo "备份目录：    ${BACKUP}"
if [ "${ASSUME_YES}" -ne 1 ]; then
  printf "继续？[y/N] "
  read -r answer
  case "${answer}" in y|Y|yes|YES) ;; *) echo "已取消"; exit 0 ;; esac
fi

mkdir -p "${APP_DIR}" "${KNOWLEDGE_DIR}" "${PG_DATA}"
# A freshly mounted filesystem may contain lost+found; only an initialized
# PostgreSQL data directory counts as existing data.
if [ -f "${PG_DATA}/PG_VERSION" ] && [ "${REUSE_DATA}" -ne 1 ]; then
  die "${PG_DATA} 已是 PostgreSQL 数据目录。迁移包是空库；确认要复用现有数据请加 --reuse-data"
fi

# Back up existing code/config only (never data/ or PG files).
mkdir -p "${BACKUP}"
if [ -d "${APP_DIR}/app" ] || [ -f "${APP_DIR}/.env" ]; then
  tar -czf "${BACKUP}/app-config.tar.gz" -C "${APP_DIR}" \
    app docker-compose.yml entrypoint.sh code-mount-entrypoint.sh .env config 2>/dev/null || true
fi
if [ -d "${KNOWLEDGE_DIR}/app" ] || [ -f "${KNOWLEDGE_DIR}/.env" ]; then
  tar -czf "${BACKUP}/knowledge-config.tar.gz" -C "${KNOWLEDGE_DIR}" \
    app scripts initdb docker-compose.yml docker-compose-pg.yml .env env_knowledge 2>/dev/null || true
fi

# Image: load the one shipped in the package when present.
if [ "${SKIP_IMAGE}" -eq 0 ]; then
  IMAGE_FILE="$(find "${PKG_ROOT}/images" -maxdepth 1 -type f -name '*.tar' 2>/dev/null | head -1 || true)"
  if [ -n "${IMAGE_FILE}" ]; then
    echo "加载镜像（tar，不需要 zstd）：${IMAGE_FILE}"
    docker load -i "${IMAGE_FILE}"
  elif find "${PKG_ROOT}/images" -maxdepth 1 -type f -name '*.tar.zst' 2>/dev/null | grep -q .; then
    die "迁移包内是 zstd 镜像，但目标环境不使用 zstd；请用新版打包脚本重新打包"
  fi
fi
IMAGE_TAG="$(grep -E '^MYCOPAW_IMAGE=' "${PKG_ROOT}/app-config/.env" | tail -1 | cut -d= -f2- || true)"
IMAGE_TAG="${IMAGE_TAG:-huizhi-runtime:2.2.1-arm64}"
docker image inspect "${IMAGE_TAG}" >/dev/null 2>&1 \
  || die "目标服务器没有镜像 ${IMAGE_TAG}，请把镜像 tar 放进 images/ 或先导入"

echo "安装代码和配置..."
rm -rf "${APP_DIR}/app" "${KNOWLEDGE_DIR}/app" "${KNOWLEDGE_DIR}/scripts" "${KNOWLEDGE_DIR}/initdb"
cp -a "${PKG_ROOT}/app" "${APP_DIR}/app"
cp -a "${PKG_ROOT}/app-config/." "${APP_DIR}/"
cp -a "${PKG_ROOT}/knowledge-service/app" "${KNOWLEDGE_DIR}/app"
cp -a "${PKG_ROOT}/knowledge-service/scripts" "${KNOWLEDGE_DIR}/scripts"
[ -d "${PKG_ROOT}/knowledge-service/initdb" ] && cp -a "${PKG_ROOT}/knowledge-service/initdb" "${KNOWLEDGE_DIR}/initdb"
cp -a "${PKG_ROOT}/knowledge-config/." "${KNOWLEDGE_DIR}/"

# LightRAG's offline tokenizer cache is a runtime asset, not user data. Without
# it the container tries to download o200k_base and exits on an isolated host.
if [ -d "${PKG_ROOT}/knowledge-assets/lightrag/tiktoken_cache" ]; then
  mkdir -p "${KNOWLEDGE_DIR}/data/lightrag/tiktoken_cache"
  cp -a "${PKG_ROOT}/knowledge-assets/lightrag/tiktoken_cache/." \
        "${KNOWLEDGE_DIR}/data/lightrag/tiktoken_cache/"
fi

# Empty application data directories; the app/knowledge service initialize them.
mkdir -p "${APP_DIR}/data/working" "${APP_DIR}/data/working.secret" \
         "${APP_DIR}/data/backups" "${APP_DIR}/data/logs"
mkdir -p "${KNOWLEDGE_DIR}/data"
chown -R 501:games "${APP_DIR}/data" "${APP_DIR}/app" \
  "${KNOWLEDGE_DIR}/app" "${KNOWLEDGE_DIR}/scripts" "${KNOWLEDGE_DIR}/initdb" \
  "${KNOWLEDGE_DIR}/data" 2>/dev/null || true
chmod 600 "${APP_DIR}/.env" "${KNOWLEDGE_DIR}/.env" 2>/dev/null || true

echo "校验固定 compose..."
docker compose --env-file "${APP_DIR}/.env" -f "${APP_COMPOSE}" config >/dev/null \
  || die "主应用 compose 配置无效"
docker compose --env-file "${KNOWLEDGE_DIR}/.env" -f "${KNOW_COMPOSE}" config >/dev/null \
  || die "知识服务 compose 配置无效"
docker compose --env-file "${KNOWLEDGE_DIR}/.env" -f "${PG_COMPOSE}" config >/dev/null \
  || die "PostgreSQL compose 配置无效"

# The postgres image owns PGDATA as its postgres user. For a fresh bind mount
# this is uid 999 (Debian pgvector image) or 70 (alpine).
if [ ! -f "${PG_DATA}/PG_VERSION" ]; then
  PG_IMAGE="$(grep -E '^KNOWLEDGE_POSTGRES_IMAGE=' "${KNOWLEDGE_DIR}/.env" | tail -1 | cut -d= -f2- || true)"
  case "${PG_IMAGE}" in
    *alpine*) PG_UID=70 ;;
    *) PG_UID=999 ;;
  esac
  chown -R "${PG_UID}:${PG_UID}" "${PG_DATA}"
fi

# Remove old application containers; keep PostgreSQL if it is already running.
for c in huizhi-assistant huizhi-knowledge-api huizhi-knowledge-worker huizhi-lightrag; do
  docker rm -f "${c}" >/dev/null 2>&1 || true
done

echo "启动 PostgreSQL..."
( cd "${KNOWLEDGE_DIR}" && docker compose --env-file .env -f docker-compose-pg.yml up -d )
for i in $(seq 1 60); do
  (exec 3<>/dev/tcp/127.0.0.1/15432) >/dev/null 2>&1 && break
  sleep 2
done
(exec 3<>/dev/tcp/127.0.0.1/15432) >/dev/null 2>&1 \
  || die "PostgreSQL 120 秒内未就绪"

echo "启动知识服务..."
( cd "${KNOWLEDGE_DIR}" && docker compose --env-file .env -f docker-compose.yml up -d )
echo "启动主应用..."
( cd "${APP_DIR}" && docker compose --env-file .env -f docker-compose.yml up -d )

wait_http() {
  local url=$1 tries=$2 i=0
  command -v curl >/dev/null 2>&1 || return 0
  while [ "${i}" -lt "${tries}" ]; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "${url}" 2>/dev/null || true)"
    case "${code}" in 200|401|403) return 0 ;; esac
    i=$((i + 1)); sleep 2
  done
  return 1
}
PORT="$(grep -E '^COPAW_PORT=' "${APP_DIR}/.env" | tail -1 | cut -d= -f2- || true)"
PORT="${PORT:-8088}"
TRIES=$((HEALTH_TIMEOUT_SECONDS / 2))
wait_http "http://127.0.0.1:${PORT}/api/healthz" "${TRIES}" \
  || die "主应用健康检查失败；docker logs huizhi-assistant"
wait_http "http://127.0.0.1:18100/health" "${TRIES}" \
  || die "知识服务健康检查失败；docker logs huizhi-knowledge-api"

echo
echo "部署完成。"
echo "主应用：http://<服务器IP>:${PORT}"
echo "知识服务：http://<服务器IP>:18100/health"
echo "PG 数据：${PG_DATA}"
echo "版本：$(head -3 "${APP_DIR}/app/VERSION" 2>/dev/null | tr '\n' ' ')"
echo "检查：docker ps --format 'table {{.Names}}\t{{.Status}}'"
