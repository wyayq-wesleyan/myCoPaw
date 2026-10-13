#!/usr/bin/env bash
# 按正确顺序启动全部服务。
#
# ⚠️ 为什么两个服务在不同的目录里执行 compose：
#    compose 的相对路径（./app、./data）是相对 **project directory** 解析的，而
#    ``--project-directory`` 会覆盖它。所以：
#      - 主应用在**包根目录**执行 → ./app = 包里的 app/、./data = 包里的 data/
#      - 知识服务在 **knowledge-service/ 里**执行 → ./app = knowledge-service/app
#    若把知识服务也用 --project-directory 指向根目录，它的 ./app 会错挂到主应用的
#    app 上（实测确认），知识服务就起不来了。两处都已在打包时验证过。
#
# 启动顺序有依赖：PostgreSQL 必须先就绪，主应用与知识服务都连它。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # 包根目录

if [[ ! -f "${HERE}/.env" ]]; then
  echo "缺少 ${HERE}/.env。请先：" >&2
  echo "  cp ${HERE}/.env.example ${HERE}/.env && vi ${HERE}/.env" >&2
  exit 1
fi
if [[ ! -f "${HERE}/knowledge-service/.env" ]]; then
  echo "缺少 ${HERE}/knowledge-service/.env。请先：" >&2
  echo "  cp ${HERE}/knowledge-service/.env.example ${HERE}/knowledge-service/.env" >&2
  exit 1
fi

wait_healthy() {
  local container="$1" tries="${2:-60}"
  echo -n "    等待 ${container} 就绪"
  local i status
  for ((i = 0; i < tries; i++)); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "${container}" 2>/dev/null || echo missing)"
    case "${status}" in
      healthy|running) echo " 就绪"; return 0 ;;
      missing)
        echo; echo "    容器 ${container} 不存在，启动失败。" >&2
        docker ps -a --filter "name=${container}" --format '      {{.Status}}' >&2 || true
        return 1 ;;
    esac
    echo -n "."
    sleep 3
  done
  echo
  echo "    等待 ${container} 超时。查看日志：docker logs --tail 80 ${container}" >&2
  return 1
}

echo "==> 1/3 启动 PostgreSQL 17（账号库 / 平台库 / 图谱库）"
(cd "${HERE}/knowledge-service" && \
  docker compose -f docker-compose.postgres.yml up -d)
wait_healthy huizhi-knowledge-postgres 60

echo "==> 2/3 启动知识服务（api + worker）"
(cd "${HERE}/knowledge-service" && \
  docker compose -f docker-compose.yml up -d)
wait_healthy huizhi-knowledge-api 40

echo "==> 3/3 启动主应用"
(cd "${HERE}" && docker compose -f docker-compose.yml up -d)
wait_healthy huizhi-assistant 60

echo
echo "==> 全部容器："
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'huizhi|NAMES' || true
port="$(grep -E '^COPAW_PORT=' "${HERE}/.env" | cut -d= -f2 | tr -d '[:space:]')"
echo
echo "访问： http://<服务器IP>:${port:-8088}/"
echo "登录： 用户名见 .env 的 QWENPAW_AUTH_USERNAME，密码为您在 .env 里设置的"
echo
echo "首次启动会在后台自动建表并迁移历史数据（见 README「首次启动会发生什么」）。"
