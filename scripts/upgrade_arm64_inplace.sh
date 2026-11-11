#!/usr/bin/env bash
# ARM64 原地升级：把最新代码覆盖进当前运行目录，保留 data/、postgres-data/
# 和 .env。不创建第二套运行目录，不修改数据目录。
#
# 典型用法（在解压后的包目录里执行）：
#   bash upgrade_arm64_inplace.sh --yes
#
# 脚本会优先从当前运行中的容器读取 compose 工作目录；读不到时回退到：
#   /data/huizhi_assistant
#   /data/knowledge-service
#
# 可选参数：
#   --app DIR            主应用目录（包含 app/ 和 data/）
#   --knowledge DIR      知识服务目录（包含 app/ 和 data/）
#   --yes                不再交互确认
#   --skip-backup        跳过备份（不建议）
#   --no-docker          只替换文件，不操作容器（测试用）
#   --allow-other        允许目标目录与当前运行容器目录不同
#   --skip-arch-check    跳过 aarch64 架构检查
#   --backup-dir DIR     指定备份目录
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 脚本在包根目录时也能用；也允许放在 scripts/ 下。
if [ ! -d "${ROOT_DIR}/app/src" ] && [ -d "${ROOT_DIR}/../app/src" ]; then
  ROOT_DIR="$(cd "${ROOT_DIR}/.." && pwd)"
fi

APP_DIR=""
KNOWLEDGE_DIR=""
ASSUME_YES=0
SKIP_BACKUP=0
NO_DOCKER=0
ALLOW_OTHER=0
SKIP_ARCH=0
BACKUP_DIR=""
PG_CONTAINER="huizhi-knowledge-postgres"
PG_PORT=15432

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --app)             APP_DIR="${2:-}"; shift 2 ;;
    --knowledge)       KNOWLEDGE_DIR="${2:-}"; shift 2 ;;
    --yes)             ASSUME_YES=1; shift ;;
    --skip-backup)     SKIP_BACKUP=1; shift ;;
    --no-docker)       NO_DOCKER=1; shift ;;
    --allow-other)     ALLOW_OTHER=1; shift ;;
    --skip-arch-check) SKIP_ARCH=1; shift ;;
    --backup-dir)      BACKUP_DIR="${2:-}"; shift 2 ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

die() {
  echo "错误：$*" >&2
  exit 1
}

detect_workdir() {
  local container=$1
  docker inspect "${container}" \
    --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
    2>/dev/null || true
}

# 容器的 compose 工作目录可能指向包根，也可能直接指向 deploy/。
normalize_app_dir() {
  local dir=$1
  if [ -d "${dir}/deploy/app" ] && [ -d "${dir}/deploy/data" ]; then
    printf '%s' "${dir}/deploy"
  else
    printf '%s' "${dir}"
  fi
}

normalize_knowledge_dir() {
  local dir=$1
  if [ -d "${dir}/knowledge-service/app" ] \
     && [ -d "${dir}/knowledge-service/data" ]; then
    printf '%s' "${dir}/knowledge-service"
  else
    printf '%s' "${dir}"
  fi
}

# ── 0. 目标目录：默认跟随当前运行容器 ──────────────────────────────────
RUNNING_APP_DIR="$(detect_workdir huizhi-assistant || true)"
RUNNING_KNOWLEDGE_DIR="$(detect_workdir huizhi-knowledge-api || true)"
if [ -z "${RUNNING_KNOWLEDGE_DIR}" ]; then
  RUNNING_KNOWLEDGE_DIR="$(detect_workdir huizhi-knowledge-worker || true)"
fi
if [ -z "${RUNNING_KNOWLEDGE_DIR}" ]; then
  RUNNING_KNOWLEDGE_DIR="$(detect_workdir huizhi-lightrag || true)"
fi
[ -n "${RUNNING_APP_DIR}" ] && RUNNING_APP_DIR="$(normalize_app_dir "${RUNNING_APP_DIR}")"
[ -n "${RUNNING_KNOWLEDGE_DIR}" ] && RUNNING_KNOWLEDGE_DIR="$(
  normalize_knowledge_dir "${RUNNING_KNOWLEDGE_DIR}"
)"

if [ -z "${APP_DIR}" ]; then
  APP_DIR="${RUNNING_APP_DIR:-/data/huizhi_assistant}"
fi
if [ -z "${KNOWLEDGE_DIR}" ]; then
  KNOWLEDGE_DIR="${RUNNING_KNOWLEDGE_DIR:-/data/knowledge-service}"
fi

APP_DIR="$(cd "${APP_DIR}" 2>/dev/null && pwd || printf '%s' "${APP_DIR}")"
KNOWLEDGE_DIR="$(cd "${KNOWLEDGE_DIR}" 2>/dev/null && pwd || printf '%s' "${KNOWLEDGE_DIR}")"

if [ -n "${RUNNING_APP_DIR}" ] && [ "${APP_DIR}" != "${RUNNING_APP_DIR}" ] \
   && [ "${ALLOW_OTHER}" -ne 1 ]; then
  cat >&2 <<EOF
当前运行中的主应用来自：
  ${RUNNING_APP_DIR}
本次指定的目标目录是：
  ${APP_DIR}

直接覆盖另一个目录会启动第二套实例并争用 8088/18100 端口。
如果确认要切换目录，请先停止旧部署，或加 --allow-other。
EOF
  exit 1
fi

if [ ! -d "${APP_DIR}/data" ]; then
  die "找不到 ${APP_DIR}/data，这不是主应用目录（或数据不在 data/ 下）"
fi
if [ ! -d "${KNOWLEDGE_DIR}/data" ]; then
  die "找不到 ${KNOWLEDGE_DIR}/data，这不是知识服务目录"
fi

ARCH="$(uname -m)"
if [ "${SKIP_ARCH}" -ne 1 ]; then
  case "${ARCH}" in
    aarch64|arm64) ;;
    *) die "当前架构是 ${ARCH}，这个补丁只用于 ARM64；x86 请勿使用" ;;
  esac
fi

# ── 1. 校验包内容 ──────────────────────────────────────────────────────
[ -d "${ROOT_DIR}/app/src" ] \
  || die "补丁不完整：${ROOT_DIR}/app/src 不存在"
[ -d "${ROOT_DIR}/knowledge-service/app" ] \
  || die "补丁不完整：${ROOT_DIR}/knowledge-service/app 不存在"
[ -f "${ROOT_DIR}/deploy/entrypoint.sh" ] \
  || die "补丁不完整：${ROOT_DIR}/deploy/entrypoint.sh 不存在"
[ -f "${ROOT_DIR}/deploy/docker-compose.code-mount.arm64.yml" ] \
  || die "补丁不完整：ARM64 compose 不存在"

if [ "${NO_DOCKER}" -eq 0 ]; then
  docker image inspect "huizhi-runtime:2.2.1-arm64" >/dev/null 2>&1 \
    || die "本机没有 huizhi-runtime:2.2.1-arm64 镜像。请先导入 ARM64 镜像，或确认镜像标签。"
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
if [ -n "${BACKUP_DIR}" ]; then
  BK="${BACKUP_DIR}"
elif [ -d /data ] && [ -w /data ]; then
  BK="/data/huizhi-update-backup-${STAMP}"
else
  BK="${ROOT_DIR}/backups/update-${STAMP}"
fi

cat <<EOF
主应用目录：  ${APP_DIR}
知识服务目录：${KNOWLEDGE_DIR}
备份目录：    ${BK}
补丁目录：    ${ROOT_DIR}

将执行：
  1. 备份旧 app/、compose、entrypoint、.env（不备份 data/）
  2. 停止并删除 app/knowledge/lightrag 容器（保留 PostgreSQL）
  3. 覆盖新代码、Compose、entrypoint
  4. 保留并合并 .env（密码/端口/模型地址不变）
  5. 启动知识服务和主应用，并做健康检查
EOF

if [ "${ASSUME_YES}" -ne 1 ]; then
  printf "继续？[y/N] "
  read -r answer
  case "${answer}" in
    y|Y|yes|YES) ;;
    *) echo "已取消，没有修改任何东西。"; exit 0 ;;
  esac
fi

port_open() {
  (exec 3<>/dev/tcp/127.0.0.1/"${PG_PORT}") >/dev/null 2>&1
}

wait_port() {
  local i=0
  while [ "${i}" -lt 60 ]; do
    if port_open; then return 0; fi
    i=$((i + 1)); sleep 2
  done
  return 1
}

wait_http() {
  local url=$1
  local i=0
  command -v curl >/dev/null 2>&1 || return 0
  while [ "${i}" -lt 60 ]; do
    if curl -fsS --max-time 3 "${url}" >/dev/null 2>&1; then
      return 0
    fi
    i=$((i + 1)); sleep 2
  done
  return 1
}

# ── 2. 备份 ────────────────────────────────────────────────────────────
mkdir -p "${BK}"
if [ "${SKIP_BACKUP}" -eq 1 ]; then
  echo "[1/5] 跳过备份（--skip-backup）"
else
  echo "[1/5] 备份到 ${BK}"
  if [ "${NO_DOCKER}" -eq 0 ] \
     && docker container inspect "${PG_CONTAINER}" >/dev/null 2>&1 \
     && [ "$(docker container inspect "${PG_CONTAINER}" --format '{{.State.Status}}')" = "running" ]; then
    if docker exec "${PG_CONTAINER}" sh -c 'pg_dumpall -U "$POSTGRES_USER"' \
        > "${BK}/postgres-all.sql"; then
      echo "      PostgreSQL -> ${BK}/postgres-all.sql"
    else
      echo "      pg_dumpall 失败（继续，代码替换不受影响）" >&2
    fi
  else
    echo "      PostgreSQL 未运行，跳过 pg_dumpall"
  fi

  app_items=()
  while IFS= read -r item; do app_items+=("${item}"); done < <(
    cd "${APP_DIR}" && ls -1d \
      app docker-compose.yml docker-compose.yaml \
      entrypoint.sh code-mount-entrypoint.sh config .env 2>/dev/null || true
  )
  if [ "${#app_items[@]}" -gt 0 ]; then
    tar -czf "${BK}/app-config.tar.gz" -C "${APP_DIR}" "${app_items[@]}"
    echo "      主应用代码/配置 -> ${BK}/app-config.tar.gz"
  fi

  know_items=()
  while IFS= read -r item; do know_items+=("${item}"); done < <(
    cd "${KNOWLEDGE_DIR}" && ls -1d \
      app scripts initdb docker-compose.yml docker-compose-pg.yml \
      docker-compose.postgres.yml .env 2>/dev/null || true
  )
  if [ "${#know_items[@]}" -gt 0 ]; then
    tar -czf "${BK}/knowledge-config.tar.gz" -C "${KNOWLEDGE_DIR}" "${know_items[@]}"
    echo "      知识服务代码/配置 -> ${BK}/knowledge-config.tar.gz"
  fi
fi

# ── 3. 停止旧容器（保留 PostgreSQL）───────────────────────────────────
echo "[2/5] 停止并删除旧应用/知识容器"
if [ "${NO_DOCKER}" -eq 0 ]; then
  for c in huizhi-assistant huizhi-knowledge-api huizhi-knowledge-worker huizhi-lightrag; do
    if docker container inspect "${c}" >/dev/null 2>&1; then
      docker rm -f "${c}" >/dev/null 2>&1 || true
      echo "      已移除 ${c}"
    fi
  done
else
  echo "      --no-docker：跳过"
fi

# ── 4. 安装新代码 ──────────────────────────────────────────────────────
echo "[3/5] 安装新代码到老目录"

rm -rf "${APP_DIR}/app"
cp -a "${ROOT_DIR}/app" "${APP_DIR}/app"

rm -rf "${KNOWLEDGE_DIR}/app" "${KNOWLEDGE_DIR}/scripts" "${KNOWLEDGE_DIR}/initdb"
cp -a "${ROOT_DIR}/knowledge-service/app" "${KNOWLEDGE_DIR}/app"
cp -a "${ROOT_DIR}/knowledge-service/scripts" "${KNOWLEDGE_DIR}/scripts"
if [ -d "${ROOT_DIR}/knowledge-service/initdb" ]; then
  cp -a "${ROOT_DIR}/knowledge-service/initdb" "${KNOWLEDGE_DIR}/initdb"
fi

# Compose：以补丁内的 ARM64 文件为准。
sed '/^name: /d' "${ROOT_DIR}/deploy/docker-compose.code-mount.arm64.yml" \
  > "${APP_DIR}/docker-compose.yml"

sed '/^name: /d' "${ROOT_DIR}/knowledge-service/docker-compose.code-mount.arm64.yml" \
  > "${KNOWLEDGE_DIR}/docker-compose.yml"
if [ -f "${ROOT_DIR}/knowledge-service/docker-compose.postgres.yml" ]; then
  sed '/^name: /d' "${ROOT_DIR}/knowledge-service/docker-compose.postgres.yml" \
    > "${KNOWLEDGE_DIR}/docker-compose.postgres.yml"
  # 老现场可能仍使用 docker-compose-pg.yml 这个名字。
  cp "${KNOWLEDGE_DIR}/docker-compose.postgres.yml" \
     "${KNOWLEDGE_DIR}/docker-compose-pg.yml"
fi

cp "${ROOT_DIR}/deploy/entrypoint.sh" \
   "${ROOT_DIR}/deploy/code-mount-entrypoint.sh" \
   "${APP_DIR}/"
if [ -d "${ROOT_DIR}/deploy/config" ]; then
  rm -rf "${APP_DIR}/config"
  cp -a "${ROOT_DIR}/deploy/config" "${APP_DIR}/config"
fi

cp "${ROOT_DIR}/deploy/.env.example" "${APP_DIR}/.env.example"
cp "${ROOT_DIR}/knowledge-service/.env.example" "${KNOWLEDGE_DIR}/.env.example"
chmod +x "${APP_DIR}/entrypoint.sh" "${APP_DIR}/code-mount-entrypoint.sh"

echo "      主应用：${APP_DIR}/app、docker-compose.yml、entrypoint.sh"
echo "      知识服务：${KNOWLEDGE_DIR}/app、docker-compose.yml"

# ── 5. 合并 .env（保留旧值）───────────────────────────────────────────
echo "[4/5] 合并 .env"

get_env() {
  local file=$1 key=$2
  [ -f "${file}" ] || return 0
  grep -E "^${key}=" "${file}" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

set_env() {
  local file=$1 key=$2 value=$3 tmp
  [ -f "${file}" ] || : > "${file}"
  tmp="$(mktemp)"
  awk -v k="${key}" -v v="${value}" '
    BEGIN { done = 0 }
    index($0, k "=") == 1 {
      if (!done) { print k "=" v; done = 1 }
      next
    }
    { print }
    END { if (!done) print k "=" v }
  ' "${file}" > "${tmp}"
  mv "${tmp}" "${file}"
}

APP_ENV="${APP_DIR}/.env"
KNOW_ENV="${KNOWLEDGE_DIR}/.env"

if [ ! -f "${APP_ENV}" ]; then
  cp "${ROOT_DIR}/deploy/.env.example" "${APP_ENV}"
  echo "      ! 老主应用 .env 不存在，已用补丁模板创建"
fi
if [ ! -f "${KNOW_ENV}" ]; then
  cp "${ROOT_DIR}/knowledge-service/.env.example" "${KNOW_ENV}"
  echo "      ! 老知识服务 .env 不存在，已用补丁模板创建"
fi

pg_db="$(get_env "${KNOW_ENV}" KNOWLEDGE_POSTGRES_DB || true)"
pg_user="$(get_env "${KNOW_ENV}" KNOWLEDGE_POSTGRES_USER || true)"
pg_pass="$(get_env "${KNOW_ENV}" KNOWLEDGE_POSTGRES_PASSWORD || true)"
pg_port="$(get_env "${KNOW_ENV}" KNOWLEDGE_POSTGRES_PORT || true)"

set_env "${APP_ENV}" MYCOPAW_IMAGE "huizhi-runtime:2.2.1-arm64"
set_env "${APP_ENV}" QWENPAW_KNOWLEDGE_URL "http://127.0.0.1:18100"
set_env "${APP_ENV}" QWENPAW_DATA_TABLES_DB_HOST "127.0.0.1"
set_env "${APP_ENV}" QWENPAW_DATA_TABLES_DB_PORT "${pg_port:-15432}"
[ -n "${pg_db}" ] && set_env "${APP_ENV}" QWENPAW_DATA_TABLES_DB_NAME "${pg_db}"
[ -n "${pg_user}" ] && set_env "${APP_ENV}" QWENPAW_DATA_TABLES_DB_USER "${pg_user}"
[ -n "${pg_pass}" ] && set_env "${APP_ENV}" QWENPAW_DATA_TABLES_DB_PASSWORD "${pg_pass}"

set_env "${KNOW_ENV}" KNOWLEDGE_IMAGE "huizhi-runtime:2.2.1-arm64"
set_env "${KNOW_ENV}" LIGHTRAG_URL "http://127.0.0.1:9621"
set_env "${KNOW_ENV}" KNOWLEDGE_POSTGRES_MAX_CONNECTIONS "300"
set_env "${KNOW_ENV}" KNOWLEDGE_HOST_LOG_DIR "./data/logs"
set_env "${APP_ENV}" QWENPAW_HOST_LOG_DIR "./data/logs"

old_dsn="$(get_env "${KNOW_ENV}" KNOWLEDGE_DATABASE_URL || true)"
if [ -n "${old_dsn}" ]; then
  set_env "${KNOW_ENV}" KNOWLEDGE_DATABASE_URL \
    "$(printf '%s' "${old_dsn}" | sed -E 's#@[^/]+/#@127.0.0.1:15432/#')"
else
  set_env "${KNOW_ENV}" KNOWLEDGE_DATABASE_URL \
    "postgresql://${pg_user:-huizhi_knowledge}:${pg_pass:-huizhi_knowledge}@127.0.0.1:${pg_port:-15432}/${pg_db:-huizhi_knowledge}"
fi
old_graph="$(get_env "${KNOW_ENV}" KNOWLEDGE_GRAPH_DATABASE_URL || true)"
if [ -n "${old_graph}" ]; then
  set_env "${KNOW_ENV}" KNOWLEDGE_GRAPH_DATABASE_URL \
    "$(printf '%s' "${old_graph}" | sed -E 's#@[^/]+/#@127.0.0.1:15432/#')"
fi

app_key="$(get_env "${APP_ENV}" QWENPAW_KNOWLEDGE_API_KEY || true)"
know_key="$(get_env "${KNOW_ENV}" KNOWLEDGE_API_KEY || true)"
key="${app_key:-${know_key}}"
if [ -z "${key}" ]; then
  key="$(get_env "${ROOT_DIR}/deploy/.env.example" QWENPAW_KNOWLEDGE_API_KEY || true)"
fi
if [ -n "${key}" ]; then
  set_env "${APP_ENV}" QWENPAW_KNOWLEDGE_API_KEY "${key}"
  set_env "${KNOW_ENV}" KNOWLEDGE_API_KEY "${key}"
  echo "      知识服务 Key 已同步"
else
  echo "      ! 两侧 Key 都为空（仅适合完全隔离内网）"
fi

# ── 6. 启动与健康检查 ─────────────────────────────────────────────────
echo "[5/5] 启动"
if [ "${NO_DOCKER}" -eq 1 ]; then
  echo "      --no-docker：跳过启动"
else
  if port_open; then
    echo "      15432 已有 PostgreSQL，沿用"
  elif docker container inspect "${PG_CONTAINER}" >/dev/null 2>&1; then
    echo "      启动已有 PostgreSQL 容器 ${PG_CONTAINER}"
    docker start "${PG_CONTAINER}" >/dev/null
  else
    pg_data="${KNOWLEDGE_DIR}/postgres-data"
    pg_image="${KNOWLEDGE_POSTGRES_IMAGE:-pgvector/pgvector:pg17}"
    if [ -d "${pg_data}" ] && [ -n "$(ls -A "${pg_data}" 2>/dev/null || true)" ]; then
      uid="$(stat -L -c '%u' "${pg_data}" 2>/dev/null || stat -L -f '%u' "${pg_data}" 2>/dev/null || echo '')"
      case "${uid}" in
        70) pg_image="postgres:17-alpine" ;;
        999) pg_image="pgvector/pgvector:pg17" ;;
        *) echo "      无法判断 PG 数据目录 uid=${uid}，请在 .env 设置 KNOWLEDGE_POSTGRES_IMAGE" >&2; exit 1 ;;
      esac
    fi
    pg_compose="docker-compose.postgres.yml"
    [ -f "${KNOWLEDGE_DIR}/${pg_compose}" ] || pg_compose="docker-compose-pg.yml"
    echo "      启动 PostgreSQL（${pg_image}）"
    ( cd "${KNOWLEDGE_DIR}" && KNOWLEDGE_POSTGRES_IMAGE="${pg_image}" \
        docker compose -f "${pg_compose}" up -d )
  fi

  if ! wait_port; then
    echo "PostgreSQL 120 秒内未就绪，升级未完成。" >&2
    echo "检查：docker logs ${PG_CONTAINER}" >&2
    exit 1
  fi

  echo "      启动知识服务"
  ( cd "${KNOWLEDGE_DIR}" && docker compose up -d )
  echo "      启动主应用"
  ( cd "${APP_DIR}" && docker compose up -d )

  port="$(get_env "${APP_ENV}" COPAW_PORT || true)"
  port="${port:-8088}"
  health_failed=0
  if wait_http "http://127.0.0.1:${port}/api/healthz"; then
    echo "      主应用健康检查通过：http://127.0.0.1:${port}/api/healthz"
  else
    health_failed=1
    echo "      ! 主应用健康检查未通过，请查看：docker logs huizhi-assistant" >&2
  fi
  if wait_http "http://127.0.0.1:18100/health"; then
    echo "      知识服务健康检查通过：http://127.0.0.1:18100/health"
  else
    health_failed=1
    echo "      ! 知识服务健康检查未通过，请查看：docker logs huizhi-knowledge-api" >&2
  fi
  if [ "${health_failed}" -ne 0 ]; then
    echo "升级文件已就位，但健康检查未通过；请先看日志，不要直接删除 data/。" >&2
    exit 1
  fi
fi

echo
echo "升级完成。备份：${BK}"
echo "检查："
echo "  docker ps --format 'table {{.Names}}\\t{{.Image}}\\t{{.Status}}'"
echo "  curl http://127.0.0.1:${port:-8088}/api/healthz"
echo "  curl http://127.0.0.1:18100/health"
echo
echo "回滚："
echo "  tar -xzf ${BK}/app-config.tar.gz -C ${APP_DIR}"
echo "  tar -xzf ${BK}/knowledge-config.tar.gz -C ${KNOWLEDGE_DIR}"
echo "  cd ${KNOWLEDGE_DIR} && docker compose up -d"
echo "  cd ${APP_DIR} && docker compose up -d"
echo "  # 如 PostgreSQL 需要恢复：docker exec -i ${PG_CONTAINER} psql -U \"\$POSTGRES_USER\" < ${BK}/postgres-all.sql"
