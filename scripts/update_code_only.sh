#!/usr/bin/env bash
# 只更新代码，不改配置、不改数据、不改目录。
#
# 用法：
#   bash /data/update_code.sh /data/huizhi-code-update-YYYYMMDD.tar.gz
#
# 做的事：
#   1. 校验更新包（如果旁边有 .sha256）
#   2. 解压到 /data 下的临时目录
#   3. 备份当前代码到 /data/huizhi_assistant_bak/<时间>/
#   4. 只替换：
#        /data/huizhi_assistant/app
#        /data/knowledge-service/app
#        /data/knowledge-service/scripts
#   5. 用固定 compose/env 文件重建主应用、知识服务
#   6. 检查 /api/healthz 和 /health，失败自动回滚
#
# 不替换：
#   .env、env_knowledge、docker-compose*.yml、entrypoint*.sh、config/、
#   data/、postgres-data/
set -euo pipefail

APP_DIR=/data/huizhi_assistant
KNOWLEDGE_DIR=/data/knowledge-service
BACKUP_ROOT=/data/huizhi_assistant_bak
APP_CONTAINER=huizhi-assistant
KNOW_CONTAINERS="huizhi-knowledge-api huizhi-knowledge-worker"

# Fixed configuration paths. Startup never relies on whatever a stale container
# was created with: missing or moved files abort before any code is replaced.
APP_COMPOSE="${APP_DIR}/docker-compose.yml"
APP_ENV="${APP_DIR}/.env"
APP_CODE_ENTRYPOINT="${APP_DIR}/code-mount-entrypoint.sh"
APP_ENTRYPOINT="${APP_DIR}/entrypoint.sh"
KNOW_COMPOSE="${KNOWLEDGE_DIR}/docker-compose.yml"
KNOW_ENV="${KNOWLEDGE_DIR}/.env"
KNOW_PG_COMPOSE="${KNOWLEDGE_DIR}/docker-compose-pg.yml"
KNOW_PG_FALLBACK="${KNOWLEDGE_DIR}/docker-compose.postgres.yml"
PKG="${1:-}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-300}"

die() {
  echo "错误：$*" >&2
  exit 1
}

port_open() {
  (exec 3<>/dev/tcp/127.0.0.1/15432) >/dev/null 2>&1
}

wait_port() {
  local i=0
  while [ "${i}" -lt 60 ]; do
    if port_open; then return 0; fi
    i=$((i + 1)); sleep 2
  done
  return 1
}

start_services() {
  local pg_compose="${KNOW_PG_COMPOSE}"
  [ -f "${pg_compose}" ] || pg_compose="${KNOW_PG_FALLBACK}"

  # Remove old containers so a stale compose project/container name cannot
  # shadow the fixed files. PostgreSQL is deliberately preserved.
  for c in huizhi-assistant huizhi-knowledge-api huizhi-knowledge-worker huizhi-lightrag; do
    docker rm -f "${c}" >/dev/null 2>&1 || true
  done

  if ! port_open; then
    echo "启动 PostgreSQL（${pg_compose}）"
    ( cd "${KNOWLEDGE_DIR}" && docker compose --env-file "${KNOW_ENV}" \
        -f "${pg_compose}" up -d ) || return 1
    wait_port || return 1
  fi

  echo "启动知识服务（${KNOW_COMPOSE}，env=${KNOW_ENV}）"
  ( cd "${KNOWLEDGE_DIR}" && docker compose --env-file "${KNOW_ENV}" \
      -f "${KNOW_COMPOSE}" up -d --force-recreate ) || return 1

  echo "启动主应用（${APP_COMPOSE}，env=${APP_ENV}）"
  ( cd "${APP_DIR}" && docker compose --env-file "${APP_ENV}" \
      -f "${APP_COMPOSE}" up -d --force-recreate ) || return 1
}

[ -n "${PKG}" ] || die "用法：bash /data/update_code.sh /data/huizhi-code-update-YYYYMMDD.tar.gz"
[ -f "${PKG}" ] || die "更新包不存在：${PKG}"
[ "$(id -u)" -eq 0 ] || die "请用 root 执行"

for d in "${APP_DIR}/app" "${APP_DIR}/data" \
         "${KNOWLEDGE_DIR}/app" "${KNOWLEDGE_DIR}/data"; do
  [ -d "${d}" ] || die "目录不存在：${d}"
done

# Fixed configuration files must exist at these exact paths. We intentionally
# fail before touching code: starting with a stale container is what made a
# missing compose/env invisible until the next recreate.
for f in "${APP_COMPOSE}" "${APP_ENV}" "${APP_CODE_ENTRYPOINT}" \
         "${APP_ENTRYPOINT}" "${KNOW_COMPOSE}" "${KNOW_ENV}"; do
  [ -f "${f}" ] || die "缺少固定配置文件：${f}"
done
if [ ! -f "${KNOW_PG_COMPOSE}" ] && [ ! -f "${KNOW_PG_FALLBACK}" ]; then
  die "缺少固定 PostgreSQL compose：${KNOW_PG_COMPOSE}（或 ${KNOW_PG_FALLBACK}）"
fi
docker compose version >/dev/null 2>&1 \
  || die "docker compose 不可用，无法按固定 compose 启动"
docker compose --env-file "${APP_ENV}" -f "${APP_COMPOSE}" config >/dev/null \
  || die "主应用 compose 配置无效：${APP_COMPOSE}"
docker compose --env-file "${KNOW_ENV}" -f "${KNOW_COMPOSE}" config >/dev/null \
  || die "知识服务 compose 配置无效：${KNOW_COMPOSE}"
PG_CHECK="${KNOW_PG_COMPOSE}"
[ -f "${PG_CHECK}" ] || PG_CHECK="${KNOW_PG_FALLBACK}"
docker compose --env-file "${KNOW_ENV}" -f "${PG_CHECK}" config >/dev/null \
  || die "知识服务 PostgreSQL compose 配置无效：${PG_CHECK}"

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d /data/.huizhi-code-update.XXXXXX)"
BK="${BACKUP_ROOT}/${STAMP}"
OLD_APP="${APP_DIR}/app.old.${STAMP}"
OLD_KNOW_APP="${KNOWLEDGE_DIR}/app.old.${STAMP}"
OLD_KNOW_SCRIPTS="${KNOWLEDGE_DIR}/scripts.old.${STAMP}"
NEW_APP="${APP_DIR}/app.new.${STAMP}"
NEW_KNOW_APP="${KNOWLEDGE_DIR}/app.new.${STAMP}"
NEW_KNOW_SCRIPTS="${KNOWLEDGE_DIR}/scripts.new.${STAMP}"
ROLLED_BACK=0

cleanup() {
  rm -rf "${WORK}" "${NEW_APP}" "${NEW_KNOW_APP}" "${NEW_KNOW_SCRIPTS}"
}
trap cleanup EXIT

# ── 1. 校验、解压 ──────────────────────────────────────────────────────
if [ -f "${PKG}.sha256" ]; then
  ( cd "$(dirname "${PKG}")" && sha256sum -c "$(basename "${PKG}").sha256" ) \
    || die "更新包校验失败"
else
  echo "提示：没有找到 ${PKG}.sha256，跳过包校验"
fi

tar -xzf "${PKG}" -C "${WORK}" || die "解压失败"
SRC_APP="${WORK}/app"
SRC_KNOWLEDGE="${WORK}/knowledge-service"
[ -d "${SRC_APP}/src" ] || die "更新包不完整：缺少 app/src"
[ -d "${SRC_KNOWLEDGE}/app" ] || die "更新包不完整：缺少 knowledge-service/app"
[ -d "${SRC_KNOWLEDGE}/scripts" ] || die "更新包不完整：缺少 knowledge-service/scripts"

# ── 2. 备份当前代码 ────────────────────────────────────────────────────
mkdir -p "${BK}" "${BK}/config"
chmod 700 "${BK}" "${BK}/config"
tar -czf "${BK}/app-code.tar.gz" -C "${APP_DIR}" app \
  || die "备份主应用代码失败"
tar -czf "${BK}/knowledge-code.tar.gz" -C "${KNOWLEDGE_DIR}" app scripts \
  || die "备份知识服务代码失败"
cp -a "${APP_COMPOSE}" "${APP_ENV}" "${APP_CODE_ENTRYPOINT}" \
      "${APP_ENTRYPOINT}" "${BK}/config/" 2>/dev/null || true
cp -a "${KNOW_COMPOSE}" "${KNOW_ENV}" "${BK}/config/" 2>/dev/null || true
[ -f "${KNOW_PG_COMPOSE}" ] && cp -a "${KNOW_PG_COMPOSE}" "${BK}/config/" 2>/dev/null || true
[ -f "${KNOW_PG_FALLBACK}" ] && cp -a "${KNOW_PG_FALLBACK}" "${BK}/config/" 2>/dev/null || true
echo "已备份当前代码和固定配置：${BK}"

# ── 3. 准备新代码（不碰旧目录）────────────────────────────────────────
cp -a "${SRC_APP}" "${NEW_APP}" \
  || die "复制主应用新代码失败"
cp -a "${SRC_KNOWLEDGE}/app" "${NEW_KNOW_APP}" \
  || die "复制知识服务新代码失败"
cp -a "${SRC_KNOWLEDGE}/scripts" "${NEW_KNOW_SCRIPTS}" \
  || die "复制知识服务脚本失败"
chown -R 501:games "${NEW_APP}" "${NEW_KNOW_APP}" "${NEW_KNOW_SCRIPTS}" 2>/dev/null || true

rollback() {
  [ "${ROLLED_BACK}" -eq 1 ] && return 0
  ROLLED_BACK=1
  echo "回滚旧代码..." >&2
  rm -rf "${APP_DIR}/app" "${KNOWLEDGE_DIR}/app" "${KNOWLEDGE_DIR}/scripts"
  mv "${OLD_APP}" "${APP_DIR}/app" 2>/dev/null || true
  mv "${OLD_KNOW_APP}" "${KNOWLEDGE_DIR}/app" 2>/dev/null || true
  mv "${OLD_KNOW_SCRIPTS}" "${KNOWLEDGE_DIR}/scripts" 2>/dev/null || true
  start_services >/dev/null 2>&1 || true
  echo "已回滚。备份仍在：${BK}" >&2
}

# ── 4. 原子切换目录 ────────────────────────────────────────────────────
mv "${APP_DIR}/app" "${OLD_APP}" || die "移动旧主应用代码失败"
mv "${NEW_APP}" "${APP_DIR}/app" || { rollback; die "切换主应用代码失败"; }

mv "${KNOWLEDGE_DIR}/app" "${OLD_KNOW_APP}" || die "移动旧知识服务代码失败"
mv "${NEW_KNOW_APP}" "${KNOWLEDGE_DIR}/app" || { rollback; die "切换知识服务代码失败"; }

mv "${KNOWLEDGE_DIR}/scripts" "${OLD_KNOW_SCRIPTS}" || die "移动旧知识服务脚本失败"
mv "${NEW_KNOW_SCRIPTS}" "${KNOWLEDGE_DIR}/scripts" || { rollback; die "切换知识服务脚本失败"; }

# ── 5. 按固定配置启动 ──────────────────────────────────────────────────
echo "按固定配置启动服务..."
start_services || { rollback; die "按固定 compose 启动失败"; }

# ── 6. 健康检查 ────────────────────────────────────────────────────────
PORT="$(grep -E '^COPAW_PORT=' "${APP_ENV}" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
PORT="${PORT:-8088}"

wait_http() {
  local url=$1
  local tries=$2
  local i=0
  if ! command -v curl >/dev/null 2>&1; then
    echo "提示：宿主机没有 curl，跳过 ${url} 健康检查"
    return 0
  fi
  while [ "${i}" -lt "${tries}" ]; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "${url}" 2>/dev/null || true)"
    case "${code}" in
      200|401|403) return 0 ;;
    esac
    i=$((i + 1))
    if [ $((i % 15)) -eq 0 ]; then
      echo "      仍在等待 ${url}（已 ${i} 次）……" >&2
    fi
    sleep 2
  done
  return 1
}

APP_TRIES=$((HEALTH_TIMEOUT_SECONDS / 2))
if ! wait_http "http://127.0.0.1:${PORT}/api/healthz" "${APP_TRIES}"; then
  echo "主应用健康检查失败，最近日志：" >&2
  docker logs --tail 80 "${APP_CONTAINER}" >&2 || true
  rollback
  exit 1
fi

if ! wait_http "http://127.0.0.1:18100/health" "${APP_TRIES}"; then
  echo "知识服务健康检查失败，最近日志：" >&2
  docker logs --tail 80 huizhi-knowledge-api >&2 || true
  rollback
  exit 1
fi

# ── 7. 收尾 ────────────────────────────────────────────────────────────
rm -rf "${OLD_APP}" "${OLD_KNOW_APP}" "${OLD_KNOW_SCRIPTS}"
echo
echo "更新完成。"
echo "备份目录：${BK}"
echo
docker ps --format 'table {{.Names}}\t{{.Status}}' \
  | grep -E 'huizhi-assistant|huizhi-knowledge' || true
echo
echo "检查："
echo "  curl http://127.0.0.1:${PORT}/api/healthz"
echo "  curl http://127.0.0.1:18100/health"
