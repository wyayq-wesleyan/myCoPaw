#!/usr/bin/env bash
# 停止 / 查看 / 重启全部服务。
#
# 用法：
#   bash scripts/stop-all.sh            停止应用与知识服务（数据库保持运行，数据不丢）
#   bash scripts/stop-all.sh --with-db  连数据库一起停（数据仍在，重新 --db 启动即可）
#   bash scripts/stop-all.sh --status   只查看状态
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

show() {
  echo "==> 容器状态："
  docker ps -a --format 'table {{.Names}}\t{{.Status}}' \
    | grep -E 'huizhi|NAMES' || echo "  （没有 huizhi 相关容器）"
  echo
  echo "==> 端口占用："
  for p in "$(grep -E '^COPAW_PORT=' "${HERE}/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')" \
           "$(grep -E '^KNOWLEDGE_PORT=' "${HERE}/knowledge-service/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')" \
           "$(grep -E '^KNOWLEDGE_POSTGRES_PORT=' "${HERE}/knowledge-service/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"; do
    [[ -z "${p}" ]] && continue
    if lsof -nP -iTCP:"${p}" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "  ${p}: 已监听"
    else
      echo "  ${p}: 未监听"
    fi
  done
}

case "${1:-}" in
  --status)
    show
    exit 0
    ;;
esac

echo "==> 停止主应用"
(cd "${HERE}" && docker compose -f docker-compose.yml down) || true

echo "==> 停止知识服务"
(cd "${HERE}/knowledge-service" && docker compose -f docker-compose.yml down) || true

if [[ "${1:-}" == "--with-db" ]]; then
  echo "==> 停止数据库"
  (cd "${HERE}/knowledge-service" && docker compose -f docker-compose.postgres.yml down) || true
else
  echo
  echo "数据库仍在运行（数据完好）。需要连数据库一起停：bash scripts/stop-all.sh --with-db"
fi

echo
show
echo "重新启动：bash scripts/start-all.sh"
