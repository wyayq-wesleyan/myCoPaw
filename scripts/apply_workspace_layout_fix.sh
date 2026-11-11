#!/usr/bin/env bash
# 小补丁：强制生成文件进 outputs/、中间代码进 scripts/，并禁止 shell 内联
# Python/SQL。只替换 7 个 Python 文件，不动 Compose、.env、data、数据库。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/data/huizhi_assistant
BACKUP_ROOT=/data/huizhi_assistant_bak
CONTAINER=huizhi-assistant
STAMP="$(date +%Y%m%d-%H%M%S)"
BK="${BACKUP_ROOT}/workspace-layout-${STAMP}"

FILES=(
  "src/qwenpaw/agents/tools/file_io.py"
  "src/qwenpaw/agents/tools/shell.py"
  "src/qwenpaw/runtime/prompt_contributors.py"
  "src/qwenpaw/sandbox/user_isolation.py"
  "src/qwenpaw/sandbox/external_gateway.py"
  "src/qwenpaw/sandbox/db_access.py"
  "src/qwenpaw/app/user_space.py"
)

die() { echo "错误：$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "请用 root 执行"
[ -d "${APP_DIR}/app/src/qwenpaw" ] || die "找不到 ${APP_DIR}/app/src/qwenpaw"
for rel in "${FILES[@]}"; do
  [ -f "${ROOT_DIR}/app/${rel}" ] || die "补丁缺少文件：app/${rel}"
done

mkdir -p "${BK}"
for rel in "${FILES[@]}"; do
  if [ -f "${APP_DIR}/app/${rel}" ]; then
    mkdir -p "${BK}/$(dirname "${rel}")"
    cp -a "${APP_DIR}/app/${rel}" "${BK}/${rel}"
  fi
  mkdir -p "${APP_DIR}/app/$(dirname "${rel}")"
  cp -a "${ROOT_DIR}/app/${rel}" "${APP_DIR}/app/${rel}"
done
chown -R 501:games \
  "${APP_DIR}/app/src/qwenpaw/agents/tools" \
  "${APP_DIR}/app/src/qwenpaw/runtime" \
  "${APP_DIR}/app/src/qwenpaw/sandbox" \
  "${APP_DIR}/app/src/qwenpaw/app" 2>/dev/null || true

# The UID/permission sandbox relies on Unix permissions for the paths outside
# the account roots. The application itself runs as root, so hardening these
# paths does not affect it, but it keeps ordinary-user shells out.
chmod -R go-rwx "${APP_DIR}/app" 2>/dev/null || true
chmod -R go-rwx "${APP_DIR}/data/working.secret" 2>/dev/null || true
chmod -R go-rwx "${APP_DIR}/data/logs" 2>/dev/null || true
chmod -R go-rwx "${APP_DIR}/data/backups" 2>/dev/null || true
for d in "${APP_DIR}/data" "${APP_DIR}/data/working" "${APP_DIR}/data/working/workspaces"; do
  [ -d "$d" ] && chmod 0711 "$d" 2>/dev/null || true
done

echo "检查容器内 libseccomp..."
if ! docker exec "${CONTAINER}" python -c "from qwenpaw.sandbox.user_isolation import _seccomp_available; raise SystemExit(0 if _seccomp_available() else 1)"; then
  echo "容器缺少 libseccomp，无法实现禁网/禁挂载，已停止。" >&2
  for rel in "${FILES[@]}"; do
    if [ -f "${BK}/${rel}" ]; then
      cp -a "${BK}/${rel}" "${APP_DIR}/app/${rel}"
    fi
  done
  chown -R 501:games "${APP_DIR}/app/src/qwenpaw" 2>/dev/null || true
  exit 1
fi

echo "收紧所有账号目录权限（防止 A 读取 B 的存量文件）..."
if ! docker exec -e HUIZHI_SANDBOX_ALLOW_UID_FALLBACK=1 "${CONTAINER}" \
    python -c "from qwenpaw.sandbox.user_isolation import prepare_all_accounts; print('prepared', prepare_all_accounts(), 'account(s)')"; then
  echo "账号目录权限收紧失败，恢复刚复制的文件..." >&2
  for rel in "${FILES[@]}"; do
    if [ -f "${BK}/${rel}" ]; then
      cp -a "${BK}/${rel}" "${APP_DIR}/app/${rel}"
    fi
  done
  chown -R 501:games "${APP_DIR}/app/src/qwenpaw" 2>/dev/null || true
  exit 1
fi

echo "已备份原文件：${BK}"
echo "重启主应用..."
docker restart "${CONTAINER}" >/dev/null

PORT="$(grep -E '^COPAW_PORT=' "${APP_DIR}/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
PORT="${PORT:-8088}"

check_health() {
  local i=0
  while [ "${i}" -lt 150 ]; do
    if command -v curl >/dev/null 2>&1; then
      local code
      code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 \
        "http://127.0.0.1:${PORT}/api/healthz" 2>/dev/null || true)"
      case "${code}" in
        200|401|403) return 0 ;;
      esac
    else
      return 0
    fi
    i=$((i + 1))
    sleep 2
  done
  return 1
}

if ! check_health; then
  echo "主应用健康检查失败，正在回滚这 7 个文件..." >&2
  for rel in "${FILES[@]}"; do
    if [ -f "${BK}/${rel}" ]; then
      cp -a "${BK}/${rel}" "${APP_DIR}/app/${rel}"
    fi
  done
  chown -R 501:games "${APP_DIR}/app/src/qwenpaw" 2>/dev/null || true
  docker restart "${CONTAINER}" >/dev/null || true
  echo "已回滚。备份：${BK}" >&2
  exit 1
fi

echo
echo "更新完成。"
echo "备份：${BK}"
grep -q "WorkspaceLayoutContributor" \
  "${APP_DIR}/app/src/qwenpaw/runtime/prompt_contributors.py" \
  && echo "检查：outputs/scripts 提示词已生效"
grep -q "_INLINE_CODE_RE" \
  "${APP_DIR}/app/src/qwenpaw/agents/tools/shell.py" \
  && echo "检查：shell 内联 Python/SQL 拦截已生效"
echo "检查：curl http://127.0.0.1:${PORT}/api/healthz"
