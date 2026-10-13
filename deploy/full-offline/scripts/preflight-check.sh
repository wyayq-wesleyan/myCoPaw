#!/usr/bin/env bash
# 部署前自检：把「部署完才发现」的问题提前暴露出来。
#
# 不修改任何东西，只读检查。建议在 load-images.sh 之后、start-all.sh 之前跑。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # 包根目录
cd "${HERE}"

pass=0; warn=0; fail=0
ok()   { echo "  [OK]   $*"; pass=$((pass+1)); }
note() { echo "  [警告] $*"; warn=$((warn+1)); }
bad()  { echo "  [错误] $*"; fail=$((fail+1)); }

echo "== 1. 运行环境 =="
if command -v docker >/dev/null 2>&1; then
  ok "docker 已安装：$(docker --version)"
else
  bad "未安装 docker"
fi
if docker compose version >/dev/null 2>&1; then
  ok "docker compose 插件可用：$(docker compose version --short 2>/dev/null || echo '')"
else
  bad "docker compose 不可用（需要 Docker 20.10+ 的 compose 插件）"
fi
if command -v zstd >/dev/null 2>&1; then
  ok "zstd 已安装（加载镜像需要）"
else
  bad "未安装 zstd：apt-get install -y zstd"
fi
arch="$(uname -m)"
case "${arch}" in
  x86_64|amd64) ok "CPU 架构 ${arch}（与本包匹配）" ;;
  *) bad "CPU 架构 ${arch} —— 本包是 x86_64(amd64) 专用，无法在 ${arch} 上运行" ;;
esac

echo "== 2. 磁盘 =="
avail_kb="$(df -Pk "${HERE}" | awk 'NR==2 {print $4}')"
avail_gb=$((avail_kb / 1024 / 1024))
if [[ ${avail_gb} -ge 80 ]]; then
  ok "可用空间 ${avail_gb}GB（建议 ≥80GB：镜像解压约 20GB + 模型/数据增长）"
else
  note "可用空间仅 ${avail_gb}GB，建议预留 80GB 以上"
fi

echo "== 3. 配置文件 =="
for f in .env knowledge-service/.env; do
  if [[ -f "${f}" ]]; then ok "${f} 存在"; else bad "${f} 不存在（cp ${f}.example ${f} 后编辑）"; fi
done

if [[ -f .env ]]; then
  if grep -qE '^QWENPAW_AUTH_PASSWORD=(请替换为强密码|请替换|Admin@123456|changeme)?$' .env; then
    bad "主应用 .env 的 QWENPAW_AUTH_PASSWORD 仍是默认/占位值 —— ★上线前必须改"
  else
    ok "QWENPAW_AUTH_PASSWORD 已设置"
  fi
  if grep -qE '^QWENPAW_AUTH_ENABLED=false' .env; then
    bad "QWENPAW_AUTH_ENABLED=false —— 生产环境必须为 true"
  else
    ok "认证已启用"
  fi
  kmain="$(grep -E '^QWENPAW_KNOWLEDGE_API_KEY=' .env | cut -d= -f2- || true)"
else
  kmain=""
fi

if [[ -f knowledge-service/.env ]]; then
  kside="$(grep -E '^KNOWLEDGE_API_KEY=' knowledge-service/.env | cut -d= -f2- || true)"
  if [[ -n "${kmain}" && -n "${kside}" && "${kmain}" != "${kside}" ]]; then
    bad "两处 KNOWLEDGE_API_KEY 不一致：主应用与知识服务的 Key 必须相同（都留空也可以）"
  else
    ok "知识服务 Key 一致（或都为空）"
  fi
  if grep -qE '^CHAT_BASE_URL=.*(模型服务器IP|example|localhost:端口)' knowledge-service/.env; then
    bad "知识服务 .env 的 CHAT_BASE_URL 仍是占位值 —— 必须填真实模型服务地址"
  elif grep -qE '^CHAT_BASE_URL=http' knowledge-service/.env; then
    ok "CHAT_BASE_URL 已填写"
  else
    bad "知识服务 .env 缺少有效的 CHAT_BASE_URL"
  fi
  if grep -qE '^(EMBEDDING_BASE_URL|EMBEDDING_MODEL)=((请填写聊天模型名|请填写嵌入模型名)|请填写|.*嵌入模型服务器IP)?$' knowledge-service/.env; then
    bad "嵌入模型配置仍是占位值 —— 嵌入不可用时上传会被拒绝（知识服务返回 409）"
  else
    ok "嵌入模型配置已填写"
  fi
  # Ports must not collide with an existing deployment on the same host.
  for port in "$(grep -E '^KNOWLEDGE_POSTGRES_PORT=' knowledge-service/.env | cut -d= -f2)" \
              "$(grep -E '^KNOWLEDGE_PORT=' knowledge-service/.env | cut -d= -f2)"; do
    [[ -z "${port}" ]] && continue
    if lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
      bad "端口 ${port} 已被占用 —— 改 .env 里的端口，或先停掉占用者"
    else
      ok "端口 ${port} 空闲"
    fi
  done
fi

# Sweep every remaining placeholder across both files, not just the keys checked
# above: an untouched CHAT_API_KEY / CHAT_MODEL / EMBEDDING_API_KEY / knowledge
# Key fails only at request time, which is much harder to diagnose after cutover.
leftovers="$(grep -nE '^[A-Za-z_][A-Za-z0-9_]*=(请替换|请填写|replace-with)' \
  .env knowledge-service/.env 2>/dev/null || true)"
if [[ -n "${leftovers}" ]]; then
  bad "以下配置仍是占位值，必须逐项填好："
  while IFS= read -r line; do echo "         ${line}"; done <<< "${leftovers}"
else
  ok "未发现遗留的占位值"
fi

echo "== 4. 镜像 =="
if docker image inspect huizhi-runtime:2.2.1-amd64 >/dev/null 2>&1; then
  img_arch="$(docker image inspect huizhi-runtime:2.2.1-amd64 --format '{{.Architecture}}')"
  if [[ "${img_arch}" == "amd64" ]]; then
    ok "huizhi-runtime:2.2.1-amd64 已加载（amd64）"
  else
    bad "huizhi-runtime:2.2.1-amd64 架构是 ${img_arch}，不是 amd64"
  fi
else
  bad "未找到 huizhi-runtime:2.2.1-amd64 —— 先执行 scripts/load-images.sh"
fi
if docker image inspect pgvector/pgvector:pg17 >/dev/null 2>&1; then
  pg_arch="$(docker image inspect pgvector/pgvector:pg17 --format '{{.Architecture}}')"
  if [[ "${pg_arch}" == "amd64" ]]; then
    ok "pgvector/pgvector:pg17 已加载（amd64）"
  else
    bad "pgvector/pgvector:pg17 架构是 ${pg_arch}，不是 amd64"
  fi
else
  bad "未找到 pgvector/pgvector:pg17 —— 先执行 scripts/load-images.sh"
fi

echo "== 5. compose 配置可解析 =="
for spec in ".:docker-compose.yml" \
            "knowledge-service:knowledge-service/docker-compose.yml" \
            "knowledge-service:knowledge-service/docker-compose.postgres.yml"; do
  dir="${spec%%:*}"; file="${spec##*:}"
  if (cd "${dir}" && docker compose -f "$(basename "${file}")" config >/dev/null 2>&1); then
    ok "解析正常：${file}"
  else
    bad "解析失败：${file}（在该目录执行 docker compose -f $(basename "${file}") config 看详细报错）"
  fi
done

echo
echo "== 结果：${pass} 项通过，${warn} 项警告，${fail} 项错误 =="
if [[ ${fail} -gt 0 ]]; then
  echo "请先修掉错误项再执行 scripts/start-all.sh"
  exit 1
fi
echo "自检通过，可以执行：bash scripts/start-all.sh"
