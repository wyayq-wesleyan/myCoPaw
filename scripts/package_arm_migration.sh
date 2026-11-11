#!/usr/bin/env bash
# Run on the CURRENT ARM server. Packages code/config (and optionally the image)
# for a fresh server. It never packages application data or PostgreSQL data.
#
#   bash package_arm_migration.sh
#   bash package_arm_migration.sh --skip-image
#
# Output: /data/huizhi-arm64-migration-YYYYMMDD.tar[.gz] + .sha256
set -euo pipefail

APP_SRC="${APP_SRC:-/data/huizhi_assistant}"
KNOW_SRC="${KNOW_SRC:-/data/knowledge-service}"
OUT_DIR="${OUT_DIR:-/data}"
WITH_IMAGE=1
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "错误：$*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-image) WITH_IMAGE=0; shift ;;
    --app) APP_SRC="${2:-}"; shift 2 ;;
    --knowledge) KNOW_SRC="${2:-}"; shift 2 ;;
    --out-dir) OUT_DIR="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "请用 root 执行"
[ -d "${APP_SRC}/app" ] || die "主应用目录不存在：${APP_SRC}/app"
[ -d "${KNOW_SRC}/app" ] || die "知识服务目录不存在：${KNOW_SRC}/app"
[ -d "${KNOW_SRC}/scripts" ] || die "知识服务 scripts 不存在"
[ -f "${APP_SRC}/.env" ] || die "缺少 ${APP_SRC}/.env"
[ -f "${APP_SRC}/docker-compose.yml" ] || die "缺少 ${APP_SRC}/docker-compose.yml"
[ -f "${KNOW_SRC}/.env" ] || die "缺少 ${KNOW_SRC}/.env"
[ -f "${KNOW_SRC}/docker-compose.yml" ] || die "缺少 ${KNOW_SRC}/docker-compose.yml"
[ -f "${SCRIPT_DIR}/deploy_arm_migration.sh" ] || die "打包脚本旁边缺少 deploy_arm_migration.sh"

# API keys must match, otherwise the fresh deployment redirects to login on the
# knowledge/data pages.
APP_KEY="$(grep -E '^QWENPAW_KNOWLEDGE_API_KEY=' "${APP_SRC}/.env" | tail -1 | cut -d= -f2- || true)"
KNOW_KEY="$(grep -E '^KNOWLEDGE_API_KEY=' "${KNOW_SRC}/.env" | tail -1 | cut -d= -f2- || true)"
[ "${APP_KEY}" = "${KNOW_KEY}" ] || die "主应用 QWENPAW_KNOWLEDGE_API_KEY 与知识服务 KNOWLEDGE_API_KEY 不一致"

STAMP="$(date +%Y%m%d-%H%M%S)"
NAME="huizhi-arm64-migration-$(date +%Y%m%d)"
mkdir -p "${OUT_DIR}"
STAGE="$(mktemp -d "${OUT_DIR}/.huizhi-migration-${STAMP}.XXXXXX")"
PKG="${STAGE}/${NAME}"

mkdir -p "${PKG}/app" "${PKG}/app-config" "${PKG}/knowledge-service" \
         "${PKG}/knowledge-config" "${PKG}/images"

echo "==> 复制主应用代码"
cp -a "${APP_SRC}/app/." "${PKG}/app/"
for f in docker-compose.yml entrypoint.sh code-mount-entrypoint.sh .env; do
  cp -a "${APP_SRC}/${f}" "${PKG}/app-config/"
done
[ -d "${APP_SRC}/config" ] && cp -a "${APP_SRC}/config" "${PKG}/app-config/"

echo "==> 复制知识服务代码和配置"
cp -a "${KNOW_SRC}/app/." "${PKG}/knowledge-service/app/"
cp -a "${KNOW_SRC}/scripts/." "${PKG}/knowledge-service/scripts/"
mkdir -p "${PKG}/knowledge-service/initdb"
if [ -d "${KNOW_SRC}/initdb" ]; then
  cp -a "${KNOW_SRC}/initdb/." "${PKG}/knowledge-service/initdb/"
fi
cp -a "${KNOW_SRC}/docker-compose.yml" "${PKG}/knowledge-config/"
cp -a "${KNOW_SRC}/.env" "${PKG}/knowledge-config/"
if [ -f "${KNOW_SRC}/env_knowledge" ]; then
  cp -a "${KNOW_SRC}/env_knowledge" "${PKG}/knowledge-config/"
fi

# LightRAG cannot start offline without the o200k_base tokenizer cache: it tries
# to download it from openaipublic.blob.core.windows.net and exits. The cache
# lives under data/ (which is otherwise never packaged), so ship it as a
# separate runtime asset and let the deploy script place it back.
TIKTOKEN_SRC="${KNOW_SRC}/data/lightrag/tiktoken_cache"
if [ -d "${TIKTOKEN_SRC}" ] && [ -n "$(ls -A "${TIKTOKEN_SRC}" 2>/dev/null)" ]; then
  mkdir -p "${PKG}/knowledge-assets/lightrag/tiktoken_cache"
  cp -a "${TIKTOKEN_SRC}/." "${PKG}/knowledge-assets/lightrag/tiktoken_cache/"
  echo "==> 已打包 LightRAG tokenizer 缓存（离线启动必需）"
else
  echo "警告：未找到 ${TIKTOKEN_SRC}；无外网服务器的 LightRAG 将无法启动。" >&2
  echo "      请先在当前服务器补上该缓存再重新打包。" >&2
fi

# PostgreSQL data is deliberately outside knowledge-service on the target.
# Normalize either compose filename to an absolute /postgres-data bind mount.
PG_SRC=""
[ -f "${KNOW_SRC}/docker-compose-pg.yml" ] && PG_SRC="${KNOW_SRC}/docker-compose-pg.yml"
[ -z "${PG_SRC}" ] && [ -f "${KNOW_SRC}/docker-compose.postgres.yml" ] && PG_SRC="${KNOW_SRC}/docker-compose.postgres.yml"
[ -n "${PG_SRC}" ] || die "缺少知识服务 PostgreSQL compose（docker-compose-pg.yml）"
sed -e 's#\./postgres-data#/postgres-data#g' \
    -e 's#"\./postgres-data"#"/postgres-data"#g' \
    "${PG_SRC}" > "${PKG}/knowledge-config/docker-compose-pg.yml"

cp -a "${SCRIPT_DIR}/deploy_arm_migration.sh" "${PKG}/deploy_arm_migration.sh"
chmod +x "${PKG}/deploy_arm_migration.sh"

cat > "${PKG}/README-迁移部署.txt" <<'EOF'
汇智小助手 ARM64 迁移包（仅程序，不含数据）

目标路径：
  /data/huizhi-assistant      主应用
  /data/knowledge-service     知识服务
  /postgres-data              PostgreSQL 数据（独立磁盘/目录）

部署：
  cd /data
  tar -xf huizhi-arm64-migration-YYYYMMDD.tar   # 或 .tar.gz
  cd huizhi-arm64-migration-YYYYMMDD
  bash deploy_arm_migration.sh

本包不包含：
  - 主应用 data/
  - 知识服务 data/
  - knowledge-service/postgres-data/
因此新服务器会得到空数据库和空数据目录，由应用首次启动自动初始化。
EOF

if [ "${WITH_IMAGE}" -eq 1 ]; then
  IMAGE="$(grep -E '^MYCOPAW_IMAGE=' "${APP_SRC}/.env" | tail -1 | cut -d= -f2- || true)"
  IMAGE="${IMAGE:-huizhi-runtime:2.2.1-arm64}"
  KNOW_IMAGE="$(grep -E '^KNOWLEDGE_IMAGE=' "${KNOW_SRC}/.env" | tail -1 | cut -d= -f2- || true)"
  KNOW_IMAGE="${KNOW_IMAGE:-${IMAGE}}"
  [ "${IMAGE}" = "${KNOW_IMAGE}" ] \
    || die "主应用镜像 ${IMAGE} 与知识服务镜像 ${KNOW_IMAGE} 不一致；请统一后再打包"
  docker image inspect "${IMAGE}" >/dev/null 2>&1 \
    || die "当前服务器没有镜像 ${IMAGE}，无法打包；可加 --skip-image"
  echo "==> 导出镜像 ${IMAGE}（未压缩 tar，目标服务器不需要 zstd）"
  docker save -o "${PKG}/images/huizhi-runtime-arm64.tar" "${IMAGE}"
fi

# tar without recompressing the already-compressed image.
echo "==> 生成迁移包"
if [ "${WITH_IMAGE}" -eq 1 ]; then
  OUT="${OUT_DIR}/${NAME}.tar"
  tar -C "${STAGE}" -cf "${OUT}" "${NAME}"
else
  OUT="${OUT_DIR}/${NAME}.tar.gz"
  tar -C "${STAGE}" -czf "${OUT}" "${NAME}"
fi
( cd "${OUT_DIR}" && sha256sum "$(basename "${OUT}")" > "$(basename "${OUT}").sha256" )
rm -rf "${STAGE}"
echo
echo "迁移包：${OUT}"
echo "校验文件：${OUT}.sha256"
