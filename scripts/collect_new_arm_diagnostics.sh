#!/usr/bin/env bash
# 汇智小助手新 ARM 服务器诊断日志收集
#
# 目标：把主应用、知识服务、LightRAG、PostgreSQL 的日志和运行现场收集到一个
# tar.gz，尽量从宿主机挂载目录和容器文件系统里取日志，不把 `docker logs` 当主来源。
#
# 用法（在服务器上，root）：
#   bash collect_new_arm_diagnostics.sh
#   bash collect_new_arm_diagnostics.sh --out-dir /data
#
# 产物：
#   /data/huizhi-diag-YYYYmmdd-HHMMSS.tar.gz
#   /data/huizhi-diag-YYYYmmdd-HHMMSS.tar.gz.sha256
#
# 说明：
#   - 不会复制 .env、auth.json、用户数据、数据库文件；
#   - .env 只输出键值和密钥指纹（sha256 前 12 位），便于比对但不泄露明文；
#   - 默认不执行 docker logs；如确实需要，设置 INCLUDE_DOCKER_LOGS=1。
set -uo pipefail
umask 077

[ "$(id -u)" -eq 0 ] || {
  echo "请用 root 执行（需要读取容器和 /postgres-data）" >&2
  exit 1
}

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${OUT_DIR:-/data}"
while [ $# -gt 0 ]; do
  case "$1" in
    --out-dir) OUT_DIR="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done
APP_DIR="${APP_DIR:-/data/huizhi-assistant}"
[ -d "${APP_DIR}/app" ] || APP_DIR="/data/huizhi_assistant"
KNOW_DIR="${KNOW_DIR:-/data/knowledge-service}"
PG_DATA="${PG_DATA:-/postgres-data}"
[ -d "${OUT_DIR}" ] || OUT_DIR="/tmp"

WORK="$(mktemp -d "${OUT_DIR}/.huizhi-diag-${STAMP}.XXXXXX" 2>/dev/null)" || {
  echo "无法在 ${OUT_DIR} 创建临时目录" >&2
  exit 1
}
PKG="${WORK}/huizhi-diag-${STAMP}"
mkdir -p "${PKG}"/{host,containers,config,http,frontend,db,logs,browser}

say() { printf '%s\n' "$*"; }
section() { printf '\n##### %s #####\n' "$*"; }

# ---------------------------------------------------------------- helpers

env_get() {
  local key="$1" file="$2" line
  [ -f "${file}" ] || return 1
  line="$(grep -E "^(export[[:space:]]+)?${key}=" "${file}" 2>/dev/null | tail -1 || true)"
  [ -n "${line}" ] || return 1
  line="${line#*=}"
  # strip one layer of matching quotes
  case "${line}" in
    \"*\") line="${line#\"}"; line="${line%\"}" ;;
    \'*\') line="${line#\'}"; line="${line%\'}" ;;
  esac
  printf '%s' "${line}"
}

fingerprint() {
  printf '%s' "${1:-}" | sha256sum 2>/dev/null | cut -c1-12
}

sanitize_stream() {
  # Reads KEY=VALUE lines from stdin, redacts secret-looking values.
  local line key val
  while IFS= read -r line; do
    case "${line}" in
      ""|\#*) printf '%s\n' "${line}"; continue ;;
    esac
    key="${line%%=*}"
    val="${line#*=}"
    case "${key}" in
      *PASSWORD*|*SECRET*|*API_KEY*|*TOKEN*|*CREDENTIAL*|*PRIVATE_KEY*)
        printf '%s=<redacted> sha256:%s len:%s\n' "${key}" "$(fingerprint "${val}")" "${#val}"
        ;;
      *)
        printf '%s=%s\n' "${key}" "${val}"
        ;;
    esac
  done
}

sanitize_env_file() {
  local file="$1" out="$2"
  : >"${out}"
  if [ ! -f "${file}" ]; then
    echo "missing: ${file}" >>"${out}"
    return 0
  fi
  echo "# sanitized from ${file}" >>"${out}"
  sanitize_stream <"${file}" >>"${out}"
}

copy_tree_capped() {
  # copy_tree_capped <src-dir> <dest-dir> [cap-mb]
  local src="$1" dest="$2" cap_mb="${3:-200}" f rel
  [ -d "${src}" ] || return 0
  mkdir -p "${dest}"
  while IFS= read -r -d '' f; do
    rel="${f#"${src}"/}"
    mkdir -p "${dest}/$(dirname "${rel}")"
    cp -a "${f}" "${dest}/${rel}" 2>/dev/null || true
  done < <(find "${src}" -type f -size -20M -print0 2>/dev/null)

  while IFS= read -r -d '' f; do
    rel="${f#"${src}"/}"
    mkdir -p "${dest}/$(dirname "${rel}")"
    tail -c $((cap_mb * 1024 * 1024)) "${f}" >"${dest}/${rel}.tail" 2>/dev/null || true
  done < <(find "${src}" -type f -size +20M -print0 2>/dev/null)
}

copy_container_dir() {
  # copy_container_dir <container> <path-in-container> <dest-dir>
  local c="$1" path="$2" dest="$3" tmp base
  docker inspect "${c}" >/dev/null 2>&1 || return 0
  tmp="${WORK}/.cp-${c//[^A-Za-z0-9_.-]/_}-$$"
  rm -rf "${tmp}"; mkdir -p "${tmp}"
  if docker cp "${c}:${path}" "${tmp}/" >/dev/null 2>&1; then
    base="$(basename "${path}")"
    if [ -e "${tmp}/${base}" ]; then
      copy_tree_capped "${tmp}/${base}" "${dest}" 200
    fi
  fi
  rm -rf "${tmp}"
}

copy_container_file() {
  # copy_container_file <container> <file-in-container> <dest-dir>
  local c="$1" path="$2" dest="$3" tmp
  docker inspect "${c}" >/dev/null 2>&1 || return 0
  tmp="${WORK}/.cpf-${c//[^A-Za-z0-9_.-]/_}-$$"
  rm -rf "${tmp}"; mkdir -p "${tmp}"
  if docker cp "${c}:${path}" "${tmp}/" >/dev/null 2>&1; then
    mkdir -p "${dest}"
    find "${tmp}" -maxdepth 1 -type f -exec cp -a {} "${dest}/" \; 2>/dev/null || true
  fi
  rm -rf "${tmp}"
}

run_to() {
  # run_to <output-file> <command...>
  local out="$1"; shift
  { echo "\$ $*"; "$@" 2>&1; echo; } >"${out}" 2>&1 || true
}

run_sh_to() {
  # run_sh_to <output-file> <shell-command>
  local out="$1" cmd="$2"
  { echo "\$ ${cmd}"; bash -c "${cmd}" 2>&1; echo; } >"${out}" 2>&1 || true
}

# ---------------------------------------------------------------- validate

section "环境检查"
say "APP_DIR=${APP_DIR}"
say "KNOW_DIR=${KNOW_DIR}"
say "PG_DATA=${PG_DATA}"
say "OUT_DIR=${OUT_DIR}"

{
  echo "hostname: $(hostname 2>/dev/null || true)"
  echo "date: $(date -Is 2>/dev/null || date)"
  echo "whoami: $(id 2>/dev/null || true)"
  echo "APP_DIR: ${APP_DIR}"
  echo "KNOW_DIR: ${KNOW_DIR}"
  echo "PG_DATA: ${PG_DATA}"
} >"${PKG}/host/summary.txt"

if [ ! -d "${APP_DIR}/app" ]; then
  echo "警告：找不到 ${APP_DIR}/app，主应用日志可能收集不全" >&2
fi
if [ ! -d "${KNOW_DIR}/app" ]; then
  echo "警告：找不到 ${KNOW_DIR}/app，知识服务日志可能收集不全" >&2
fi

# ---------------------------------------------------------------- system

section "收集系统信息"
run_sh_to "${PKG}/host/system.txt" "uname -a; echo; cat /etc/os-release 2>/dev/null; echo; uptime; echo; free -h; echo; df -h; echo; df -i"
run_sh_to "${PKG}/host/docker-version.txt" "docker version 2>&1; echo; docker compose version 2>&1"
run_sh_to "${PKG}/host/dmesg-tail.txt" "dmesg 2>/dev/null | tail -300"
run_sh_to "${PKG}/host/dmesg-oom.txt" "dmesg 2>/dev/null | grep -iE 'oom|out of memory|killed process|landlock' | tail -100"
run_sh_to "${PKG}/host/docker-service.txt" "systemctl status docker --no-pager 2>&1 | tail -80; echo; journalctl -u docker --since '1 day ago' --no-pager 2>/dev/null | tail -500"
run_sh_to "${PKG}/host/docker-df.txt" "docker system df 2>&1"

# ---------------------------------------------------------------- network

section "收集网络与端口"
run_sh_to "${PKG}/host/network.txt" "ip addr 2>&1; echo; ip route 2>&1; echo; cat /etc/resolv.conf 2>/dev/null; echo; env | grep -i proxy || true"
run_sh_to "${PKG}/host/listen.txt" "ss -ltnp 2>&1 | head -200"
run_sh_to "${PKG}/host/firewall.txt" "command -v firewall-cmd >/dev/null && { firewall-cmd --state 2>&1; firewall-cmd --list-all 2>&1; } || echo 'firewall-cmd not found'; echo; command -v getenforce >/dev/null && getenforce 2>&1 || true; echo; iptables -S 2>&1 | head -300 || true"

# ---------------------------------------------------------------- containers

section "收集容器现场"
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}' >"${PKG}/containers/docker-ps.txt" 2>&1 || true
docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E 'huizhi|knowledge|middleware|postgres|lightrag' | sort -u >"${PKG}/containers/names.txt" || true

while IFS= read -r c; do
  [ -n "${c}" ] || continue
  safe="$(printf '%s' "${c}" | tr -c 'A-Za-z0-9_.-' '_')"
  dest="${PKG}/containers/${safe}"
  mkdir -p "${dest}/logs"

  docker inspect --format \
    'Name: {{.Name}}
Image: {{.Config.Image}}
State: {{.State.Status}}
Restarting: {{.State.Restarting}}
ExitCode: {{.State.ExitCode}}
StartedAt: {{.State.StartedAt}}
FinishedAt: {{.State.FinishedAt}}
RestartCount: {{.RestartCount}}
NetworkMode: {{.HostConfig.NetworkMode}}
Health: {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "${c}" >"${dest}/state.txt" 2>&1 || true

  docker inspect --format '{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' \
    "${c}" >"${dest}/mounts.txt" 2>&1 || true

  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
    "${c}" 2>/dev/null | sanitize_stream >"${dest}/env-sanitized.txt" || true

  # ---- host-side bind mounts: copy any mount source that looks like logs
  while IFS='|' read -r src dst; do
    [ -n "${src}" ] || continue
    case "${dst}" in
      # These are copied explicitly below from the host side; skip duplicates.
      /app/working/logs|/data/logs|/data/lightrag/logs) continue ;;
    esac
    case "${dst}" in
      *log*|*Log*|*elcLog*)
        [ -d "${src}" ] && copy_tree_capped "${src}" "${dest}/host-mounts/$(printf '%s' "${dst}" | tr '/' '_')" 200
        ;;
    esac
  done < <(docker inspect --format '{{range .Mounts}}{{.Source}}|{{.Destination}}{{"\n"}}{{end}}' "${c}" 2>/dev/null || true)

  # ---- common log paths inside the container (docker cp, not docker logs)
  for p in /app/working/logs /app/logs /app/working.backups/logs \
           /data/logs /data/lightrag/logs /opt/huizhi-knowledge/logs \
           /var/log/supervisor /var/log/nginx /usr/elcLog; do
    copy_container_dir "${c}" "${p}" "${dest}/logs/$(printf '%s' "${p}" | tr '/' '_')"
  done

  # ---- lightrag: collect only *.log under /data/lightrag (avoid vector data)
  if [ "${c}" = "huizhi-lightrag" ]; then
    while IFS= read -r f; do
      [ -n "${f}" ] || continue
      copy_container_file "${c}" "${f}" "${dest}/logs/lightrag-files"
    done < <(docker exec "${c}" sh -c 'find /data/lightrag -maxdepth 3 -type f -name "*.log" 2>/dev/null' 2>/dev/null || true)
  fi

  # ---- optional small docker logs fallback
  if [ "${INCLUDE_DOCKER_LOGS:-0}" = "1" ]; then
    docker logs --tail 300 "${c}" >"${dest}/docker-logs-tail.txt" 2>&1 || true
  fi
done <"${PKG}/containers/names.txt"

# ---------------------------------------------------------------- host logs

section "收集宿主机挂载日志"
copy_tree_capped "${APP_DIR}/data/logs"                 "${PKG}/logs/app"          300
copy_tree_capped "${APP_DIR}/data/working/logs"         "${PKG}/logs/app-working"  200
copy_tree_capped "${KNOW_DIR}/data/logs"                "${PKG}/logs/knowledge"    300

# LightRAG: only log files / logs directory, never the vector store.
mkdir -p "${PKG}/logs/lightrag"
if [ -d "${KNOW_DIR}/data/lightrag" ]; then
  while IFS= read -r f; do
    [ -n "${f}" ] || continue
    rel="${f#"${KNOW_DIR}/data/lightrag"/}"
    mkdir -p "${PKG}/logs/lightrag/$(dirname "${rel}")"
    if [ "$(stat -c %s "${f}" 2>/dev/null || echo 0)" -gt $((20 * 1024 * 1024)) ]; then
      tail -c $((100 * 1024 * 1024)) "${f}" >"${PKG}/logs/lightrag/${rel}.tail" 2>/dev/null || true
    else
      cp -a "${f}" "${PKG}/logs/lightrag/${rel}" 2>/dev/null || true
    fi
  done < <(find "${KNOW_DIR}/data/lightrag" -maxdepth 3 -type f -name '*.log' 2>/dev/null)
  copy_tree_capped "${KNOW_DIR}/data/lightrag/logs" "${PKG}/logs/lightrag/logs" 200
fi

# PostgreSQL logs (only if logging_collector writes them under PGDATA)
mkdir -p "${PKG}/logs/postgres"
for d in "${PG_DATA}/log" "${PG_DATA}/pg_log"; do
  [ -d "${d}" ] || continue
  while IFS= read -r f; do
    [ -n "${f}" ] || continue
    rel="${f#"${d}"/}"
    mkdir -p "${PKG}/logs/postgres/$(dirname "${rel}")"
    if [ "$(stat -c %s "${f}" 2>/dev/null || echo 0)" -gt $((20 * 1024 * 1024)) ]; then
      tail -c $((100 * 1024 * 1024)) "${f}" >"${PKG}/logs/postgres/${rel}.tail" 2>/dev/null || true
    else
      cp -a "${f}" "${PKG}/logs/postgres/${rel}" 2>/dev/null || true
    fi
  done < <(find "${d}" -maxdepth 2 -type f \( -name '*.log' -o -name '*.csv' -o -name 'postgresql-*' \) 2>/dev/null)
done

# ---------------------------------------------------------------- config

section "收集配置（脱敏）"
sanitize_env_file "${APP_DIR}/.env" "${PKG}/config/app-env-sanitized.txt"
sanitize_env_file "${KNOW_DIR}/.env" "${PKG}/config/knowledge-env-sanitized.txt"

for f in docker-compose.yml docker-compose-pg.yml docker-compose.postgres.yml \
         entrypoint.sh code-mount-entrypoint.sh; do
  [ -f "${APP_DIR}/${f}" ] && cp -a "${APP_DIR}/${f}" "${PKG}/config/app-${f}" 2>/dev/null || true
  [ -f "${KNOW_DIR}/${f}" ] && cp -a "${KNOW_DIR}/${f}" "${PKG}/config/knowledge-${f}" 2>/dev/null || true
done

# Key comparisons that matter for the login/knowledge 401 class of failures.
APP_KEY="$(env_get QWENPAW_KNOWLEDGE_API_KEY "${APP_DIR}/.env" || true)"
KNOW_KEY="$(env_get KNOWLEDGE_API_KEY "${KNOW_DIR}/.env" || true)"
{
  echo "APP QWENPAW_KNOWLEDGE_API_KEY fingerprint: $(fingerprint "${APP_KEY}") len:${#APP_KEY}"
  echo "KNOW KNOWLEDGE_API_KEY fingerprint:        $(fingerprint "${KNOW_KEY}") len:${#KNOW_KEY}"
  if [ -n "${APP_KEY}" ] && [ "${APP_KEY}" = "${KNOW_KEY}" ]; then
    echo "MATCH: yes"
  else
    echo "MATCH: no"
  fi
  echo
  echo "COPAW_PORT: $(env_get COPAW_PORT "${APP_DIR}/.env" || echo 8088)"
  echo "QWENPAW_AUTH_ENABLED: $(env_get QWENPAW_AUTH_ENABLED "${APP_DIR}/.env" || echo '<unset>')"
  echo "QWENPAW_FORCE_AUTH_ON_LOCALHOST: $(env_get QWENPAW_FORCE_AUTH_ON_LOCALHOST "${APP_DIR}/.env" || echo '<unset>')"
  echo "QWENPAW_AUTH_USERNAME: $(env_get QWENPAW_AUTH_USERNAME "${APP_DIR}/.env" || echo '<unset>')"
  echo "QWENPAW_DATA_TABLES_DB_HOST: $(env_get QWENPAW_DATA_TABLES_DB_HOST "${APP_DIR}/.env" || echo '<unset>')"
  echo "QWENPAW_DATA_TABLES_DB_PORT: $(env_get QWENPAW_DATA_TABLES_DB_PORT "${APP_DIR}/.env" || echo '<unset>')"
  echo "KNOWLEDGE_POSTGRES_PORT: $(env_get KNOWLEDGE_POSTGRES_PORT "${KNOW_DIR}/.env" || echo '<unset>')"
  echo "KNOWLEDGE_DATABASE_URL: $(env_get KNOWLEDGE_DATABASE_URL "${KNOW_DIR}/.env" | sed -E 's#(://[^:]+:)[^@]+@#\1<redacted>@#' || echo '<unset>')"
} >"${PKG}/config/key-check.txt"

# auth.json metadata only (never copy hashes/passwords)
AUTH_FILE="${APP_DIR}/data/working.secret/auth.json"
{
  echo "path: ${AUTH_FILE}"
  if [ -f "${AUTH_FILE}" ]; then
    ls -l "${AUTH_FILE}"
    if command -v python3 >/dev/null 2>&1; then
      python3 - "${AUTH_FILE}" <<'PY' 2>&1 || true
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p, "r", encoding="utf-8"))
except Exception as exc:
    print("parse: failed:", exc)
    raise SystemExit
users = d.get("users") if isinstance(d.get("users"), dict) else {}
print("users:", len(users))
for name, rec in users.items():
    if isinstance(rec, dict):
        print(" - role=%s enabled=%s" % (rec.get("role"), rec.get("enabled")))
print("has_jwt_secret:", bool(d.get("jwt_secret")))
print("has_legacy_user:", isinstance(d.get("user"), dict))
PY
    fi
  else
    echo "auth.json not found"
  fi
} >"${PKG}/config/auth-metadata.txt"

# ---------------------------------------------------------------- HTTP

section "HTTP 探测"
PORT="$(env_get COPAW_PORT "${APP_DIR}/.env" || true)"; PORT="${PORT:-8088}"
HOST_IP="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -vE '^127\.' | head -1 || true)"
[ -n "${HOST_IP}" ] || HOST_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{for(i=1;i<=NF;i++) if($i=="src") print $(i+1); exit}' || true)"
[ -n "${HOST_IP}" ] || HOST_IP="127.0.0.1"

{
  echo "PORT=${PORT}"
  echo "HOST_IP=${HOST_IP}"
} >"${PKG}/http/targets.txt"

probe() {
  local title="$1"; shift
  {
    echo "##### ${title}"
    echo "\$ curl $*"
    curl -sS -D - -o - --max-time 10 "$@" 2>&1 || true
    echo
  } >>"${PKG}/http/curl.txt"
}

: >"${PKG}/http/curl.txt"
probe "healthz via 127.0.0.1" "http://127.0.0.1:${PORT}/api/healthz"
probe "auth/status via 127.0.0.1" "http://127.0.0.1:${PORT}/api/auth/status"
probe "version via 127.0.0.1" "http://127.0.0.1:${PORT}/api/version"
probe "root via 127.0.0.1" "http://127.0.0.1:${PORT}/"
probe "settings/language via 127.0.0.1" "http://127.0.0.1:${PORT}/api/settings/language"
probe "settings/upload-limit via 127.0.0.1" "http://127.0.0.1:${PORT}/api/settings/upload-limit"
probe "auth/status via ${HOST_IP}" "http://${HOST_IP}:${PORT}/api/auth/status"
probe "root via ${HOST_IP}" "http://${HOST_IP}:${PORT}/"
probe "OPTIONS login (CORS preflight)" -X OPTIONS "http://127.0.0.1:${PORT}/api/auth/login" \
  -H "Origin: http://${HOST_IP}:${PORT}" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type"
probe "POST login with fake user" -X POST "http://127.0.0.1:${PORT}/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"__diag_probe__","password":"__diag_probe__"}'

# Headers of the first asset referenced by index.html
ASSET="$(grep -oE 'src="[^"]+"' "${APP_DIR}/app/console/dist/index.html" 2>/dev/null | head -1 | cut -d'"' -f2 || true)"
if [ -n "${ASSET}" ]; then
  probe "first asset ${ASSET}" "http://127.0.0.1:${PORT}${ASSET}"
fi

# ---------------------------------------------------------------- frontend

section "检查前端构建产物"
FRONT="${APP_DIR}/app/console/dist"
{
  echo "dist: ${FRONT}"
  ls -la "${FRONT}" 2>&1 || true
  echo
  echo "index.html:"
  sed -n '1,120p' "${FRONT}/index.html" 2>/dev/null || true
} >"${PKG}/frontend/index-summary.txt"

if [ -d "${FRONT}/assets" ]; then
  grep -rhoE 'https?://[0-9A-Za-z._:-]+' "${FRONT}/assets" 2>/dev/null \
    | sort -u | head -300 >"${PKG}/frontend/absolute-urls.txt" || true
  grep -rlE '127\.0\.0\.1|localhost:[0-9]+' "${FRONT}/assets" 2>/dev/null \
    | head -50 >"${PKG}/frontend/localhost-refs.txt" || true
  ls -l "${FRONT}/assets" 2>/dev/null | head -100 >"${PKG}/frontend/assets-list.txt" || true
fi

# ---------------------------------------------------------------- database

section "检查 PostgreSQL"
PG_USER="$(env_get KNOWLEDGE_POSTGRES_USER "${KNOW_DIR}/.env" || true)"; PG_USER="${PG_USER:-huizhi_knowledge}"
PG_DB="$(env_get KNOWLEDGE_POSTGRES_DB "${KNOW_DIR}/.env" || true)"; PG_DB="${PG_DB:-huizhi_knowledge}"
PG_PASS="$(env_get KNOWLEDGE_POSTGRES_PASSWORD "${KNOW_DIR}/.env" || true)"
PG_CONTAINER="huizhi-knowledge-postgres"

{
  echo "PG container: ${PG_CONTAINER}"
  echo "PG user: ${PG_USER}"
  echo "PG db: ${PG_DB}"
  echo
  docker exec "${PG_CONTAINER}" pg_isready -U "${PG_USER}" -d "${PG_DB}" 2>&1 || true
  echo
  docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${PG_DB}" -Atc \
    "select version(); show port; show listen_addresses; show logging_collector; show log_directory;" 2>&1 || true
  echo
  if [ -n "${PG_PASS}" ]; then
    docker exec -e PGPASSWORD="${PG_PASS}" "${PG_CONTAINER}" \
      psql -h 127.0.0.1 -U "${PG_USER}" -d "${PG_DB}" -Atc \
      "select 'documents', count(*) from documents;" 2>&1 || true
    echo
    docker exec -e PGPASSWORD="${PG_PASS}" "${PG_CONTAINER}" \
      psql -h 127.0.0.1 -U "${PG_USER}" -d "${PG_DB}" -c '\dt' 2>&1 || true
  fi
  echo
  docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${PG_DB}" -Atc \
    "select state, count(*) from pg_stat_activity group by state;" 2>&1 || true
} >"${PKG}/db/postgres-check.txt"

{
  ls -ld "${PG_DATA}" 2>&1 || true
  ls -la "${PG_DATA}" 2>&1 | head -60 || true
  [ -f "${PG_DATA}/PG_VERSION" ] && echo "PG_VERSION=$(cat "${PG_DATA}/PG_VERSION")" || true
  du -sh "${PG_DATA}" 2>/dev/null || true
} >"${PKG}/db/postgres-data.txt"

# ---------------------------------------------------------------- file layout

section "检查目录与权限"
{
  echo "== APP =="
  ls -la "${APP_DIR}" 2>&1 || true
  echo
  echo "== APP data =="
  ls -la "${APP_DIR}/data" 2>&1 || true
  echo
  echo "== KNOW =="
  ls -la "${KNOW_DIR}" 2>&1 || true
  echo
  echo "== KNOW data =="
  ls -la "${KNOW_DIR}/data" 2>&1 || true
} >"${PKG}/host/layout.txt"

# ---------------------------------------------------------------- browser note

cat >"${PKG}/browser/BROWSER-HAR-HOWTO.txt" <<'EOF'
浏览器侧信息（服务端日志不会记录 Failed to fetch 的原因，必须带这个回来）
=====================================================================

1. 用 Chrome/Edge 打开登录页，按 F12 -> Network。
2. 勾选 "Preserve log"，然后复现一次登录失败。
3. 在请求列表里找到失败的那条（通常是 /api/auth/login 或 /api/auth/status），
   记录：
   - Request URL（完整地址，含协议、主机、端口、路径）
   - Status（例如 (failed) / net::ERR_CONNECTION_REFUSED / CORS error）
   - Request Headers 里的 Origin
4. 右键请求列表 -> "Save all as HAR with content"，保存为 login-fail.har。
5. Console 标签页右键 -> "Save as..."，保存控制台输出。
6. 另外记录：
   - 浏览器地址栏完整 URL
   - 是否通过 nginx/网关/域名访问
   - 浏览器所在电脑执行 `curl -v http://<服务器IP>:<端口>/api/auth/status` 的结果

把 HAR、Console 输出和上面几项一起发回来。
EOF

# ---------------------------------------------------------------- manifest

section "生成清单"
{
  echo "package: huizhi-diag-${STAMP}"
  echo "generated: $(date -Is 2>/dev/null || date)"
  echo "host: $(hostname 2>/dev/null || true)"
  echo "app_dir: ${APP_DIR}"
  echo "knowledge_dir: ${KNOW_DIR}"
  echo "pg_data: ${PG_DATA}"
  echo
  echo "== files =="
  find "${PKG}" -type f -printf '%10s  %p\n' 2>/dev/null | sort -n || true
} >"${PKG}/MANIFEST.txt"

# ---------------------------------------------------------------- package

OUT="${OUT_DIR}/huizhi-diag-${STAMP}.tar.gz"
section "打包"
tar -czf "${OUT}" -C "${WORK}" "$(basename "${PKG}")" 2>/dev/null || {
  echo "打包失败：${OUT}" >&2
  echo "临时目录保留在 ${WORK}" >&2
  exit 1
}
sha256sum "${OUT}" >"${OUT}.sha256" 2>/dev/null || true
chmod 600 "${OUT}" "${OUT}.sha256" 2>/dev/null || true
rm -rf "${WORK}"

say ""
say "诊断包：${OUT}"
say "校验和：${OUT}.sha256"
say "大小：$(du -h "${OUT}" 2>/dev/null | cut -f1)"
say ""
say "把这两个文件拿回去即可。"
say "如果包很大，先看 ${OUT}.sha256 和大小，再决定是否只取 logs/ 子目录。"
