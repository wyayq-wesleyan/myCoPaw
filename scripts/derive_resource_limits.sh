#!/usr/bin/env bash
# 按宿主机实际容量推导各容器的资源上限，写入两套部署的 .env。
#
# 为什么需要这个脚本：`.env` 只能写常量，Compose 不做算术，所以「按机器规格配」
# 这件事必须有个生成步骤，否则每次换服务器都要手算一遍，还容易漏项（漏掉
# KNOWLEDGE_WORKER_PIDS_LIMIT 就退回 256，而解析正是在 worker 里跑的）。
#
# 用法：
#   bash scripts/derive_resource_limits.sh                # 探测并写入
#   bash scripts/derive_resource_limits.sh --dry-run       # 只打印，不落盘
#   bash scripts/derive_resource_limits.sh --cores 256 --mem-gb 1512
#   bash scripts/derive_resource_limits.sh --cpu-budget-pct 55 --mem-budget-pct 35
#
# 写入位置：`deploy/.env` 与 `knowledge-service/.env` 的受管区块。区块以
# `# >>> huizhi resource limits >>>` / `# <<< huizhi resource limits <<<` 标记，
# 重复执行会原地替换，不会堆积，也不会碰你在区块之外写的其它变量。
#
# 设计原则：
#   1. **上限是安全阀，不是目标。** 给足余量但不占满整机，宿主还需要跑模型服务、
#      Docker 自身与系统进程。
#   2. **pids 从线程数推。** 容器 pids 计的是「任务数」= 进程 + 线程。解析会拉起
#      MinerU（PyTorch 按 CPU 配额开线程）与 soffice（一棵进程树），配额必须覆盖
#      这些，否则会出现 `Cannot fork` / `can't start new thread`。
#   3. **线程数不在这里配。** 代码读 cgroup 的 CPU 配额自行推导（见
#      knowledge-service/app/parsers.py::_cpu_budget），所以改 `*_CPU_LIMIT`
#      就会自动生效，不需要同步改第二个地方。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── 参数与探测 ────────────────────────────────────────────────────────────
CORES=""
MEM_GB=""
CPU_BUDGET_PCT=55      # 五套容器 CPU 配额合计占宿主机核数的比例
MEM_BUDGET_PCT=35      # 五套容器内存上限合计占宿主机内存的比例
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --cores) CORES="$2"; shift 2 ;;
    --mem-gb) MEM_GB="$2"; shift 2 ;;
    --cpu-budget-pct) CPU_BUDGET_PCT="$2"; shift 2 ;;
    --mem-budget-pct) MEM_BUDGET_PCT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

detect_cores() {
  if [ -n "${HUIZHI_HOST_CORES:-}" ]; then echo "$HUIZHI_HOST_CORES"; return; fi
  if command -v nproc >/dev/null 2>&1; then nproc; return; fi
  if command -v sysctl >/dev/null 2>&1; then
    sysctl -n hw.ncpu 2>/dev/null && return
  fi
  getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4
}

detect_mem_gb() {
  if [ -n "${HUIZHI_HOST_MEM_GB:-}" ]; then echo "$HUIZHI_HOST_MEM_GB"; return; fi
  if [ -r /proc/meminfo ]; then
    awk '/^MemTotal:/ { printf "%d", $2 / 1024 / 1024 }' /proc/meminfo
    return
  fi
  if command -v sysctl >/dev/null 2>&1; then
    local bytes
    bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
    if [ "$bytes" -gt 0 ]; then echo $(( bytes / 1024 / 1024 / 1024 )); return; fi
  fi
  echo 16
}

[ -z "$CORES" ] && CORES="$(detect_cores)"
[ -z "$MEM_GB" ] && MEM_GB="$(detect_mem_gb)"

# ── 由宿主机容量推导预算 ──────────────────────────────────────────────────
CPU_BUDGET=$(( CORES * CPU_BUDGET_PCT / 100 ))
MEM_BUDGET=$(( MEM_GB * MEM_BUDGET_PCT / 100 ))

# 小机器的下限：开发机 / 单机测试也不能算出 0
[ "$CPU_BUDGET" -lt 6 ] && CPU_BUDGET=6
[ "$MEM_BUDGET" -lt 12 ] && MEM_BUDGET=12

# ── 预算分配（合计 100%）─────────────────────────────────────────────────
# 权重依据实际负载：
#   assistant  交互请求最密，模型调用在外部服务，本地主要是编排与工具执行
#   worker     串行解析，但 MinerU 是 CPU 密集，给足核心能显著缩短单文档耗时
#   api        只处理 HTTP 与聊天附件解析，并发由解析闸门（默认 2）限制
#   lightrag   图谱服务，多数部署甚至未启用
#   postgres   连接数与查询并发
#
# 每项格式：名称 cpus 份额 mem 份额  pids 下限
SERVICES="assistant 40 45 4096
worker 30 30 512
api 12 9 512
lightrag 9 8 512
postgres 9 8 512"

# pids 规则：每个解析进程按「线程数 ≈ CPU 配额」估算，再乘并发与进程树开销。
# worker 串行 → 1 份；api 解析闸门默认 2 → 2 份。
pids_for() {
  local cpus=$1
  local concurrency=$2
  # MinerU 一次解析 = 主进程 + 线程池（约等于 CPU 配额）+ 若干子进程（soffice/图像处理），
  # 因此按 cpus × concurrency × 4 起算，再加固定的解释器/连接开销。
  local computed=$(( cpus * concurrency * 4 + 512 ))
  local floor=$3
  [ "$computed" -lt "$floor" ] && computed=$floor
  # 向上取整到 128 的倍数，读起来整齐
  echo $(( (computed + 127) / 128 * 128 ))
}

field() { echo "$SERVICES" | awk -v svc="$1" -v col="$2" '$1==svc {print $col}'; }
ASSIST_CPU=$(field assistant 2); ASSIST_MEM=$(field assistant 3); ASSIST_PIDS_FLOOR=$(field assistant 4)
WORKER_CPU=$(field worker 2);    WORKER_MEM=$(field worker 3);    WORKER_PIDS_FLOOR=$(field worker 4)
API_CPU=$(field api 2);          API_MEM=$(field api 3);          API_PIDS_FLOOR=$(field api 4)
LR_CPU=$(field lightrag 2);      LR_MEM=$(field lightrag 3);      LR_PIDS_FLOOR=$(field lightrag 4)
PG_CPU=$(field postgres 2);      PG_MEM=$(field postgres 3);      PG_PIDS_FLOOR=$(field postgres 4)

share_cpu() { local cpu=$(( CPU_BUDGET * $1 / 100 )); [ "$cpu" -lt 1 ] && cpu=1; echo "$cpu"; }
share_mem() { local mem=$(( MEM_BUDGET * $1 / 100 )); [ "$mem" -lt 1 ] && mem=1; echo "$mem"; }

A_CPU=$(share_cpu "$ASSIST_CPU"); A_MEM=$(share_mem "$ASSIST_MEM")
W_CPU=$(share_cpu "$WORKER_CPU"); W_MEM=$(share_mem "$WORKER_MEM")
K_CPU=$(share_cpu "$API_CPU");    K_MEM=$(share_mem "$API_MEM")
L_CPU=$(share_cpu "$LR_CPU");     L_MEM=$(share_mem "$LR_MEM")
P_CPU=$(share_cpu "$PG_CPU");     P_MEM=$(share_mem "$PG_MEM")

A_PIDS=$(pids_for "$A_CPU" 4 "$ASSIST_PIDS_FLOOR")
W_PIDS=$(pids_for "$W_CPU" 1 "$WORKER_PIDS_FLOOR")
K_PIDS=$(pids_for "$K_CPU" 2 "$API_PIDS_FLOOR")
L_PIDS=$(pids_for "$L_CPU" 1 "$LR_PIDS_FLOOR")
# PostgreSQL：每条连接一个后端进程，pids 必须高于 max_connections
PG_MAX_CONN=$(( MEM_BUDGET > 64 ? 300 : 150 ))
P_PIDS=$(pids_for "$P_CPU" 1 "$PG_PIDS_FLOOR")
[ "$P_PIDS" -lt $(( PG_MAX_CONN + 128 )) ] && P_PIDS=$(( (PG_MAX_CONN + 128 + 127) / 128 * 128 ))

# LLM 并发：按 assistant 的 CPU 配额推，一个请求本地开销远小于一个核，
# 但不能超过模型服务能承受的量（外部服务，由部署方另行确认）。
A_LLM=$(( A_CPU / 4 ))
[ "$A_LLM" -lt 4 ] && A_LLM=4
[ "$A_LLM" -gt 32 ] && A_LLM=32

# ── 生成区块 ──────────────────────────────────────────────────────────────
BEGIN_MARK="# >>> huizhi resource limits >>>"
END_MARK="# <<< huizhi resource limits <<<"

ASSIST_BLOCK=$(cat <<EOF
$BEGIN_MARK
# 由 scripts/derive_resource_limits.sh 生成，勿手工编辑（改参数后重新运行）。
# 探测到的宿主机：${CORES} 核 / ${MEM_GB} GB
# 预算：CPU ${CPU_BUDGET_PCT}% = ${CPU_BUDGET} 核，内存 ${MEM_BUDGET_PCT}% = ${MEM_BUDGET} GB
# 合计占用：CPU ${A_CPU}/${CPU_BUDGET}，内存 ${A_MEM}/${MEM_BUDGET} GB（其余留给宿主与模型服务）
QWENPAW_CPU_LIMIT=${A_CPU}
QWENPAW_MEMORY_LIMIT=${A_MEM}g
QWENPAW_PIDS_LIMIT=${A_PIDS}
QWENPAW_LLM_MAX_CONCURRENT=${A_LLM}
$END_MARK
EOF
)

KNOWLEDGE_BLOCK=$(cat <<EOF
$BEGIN_MARK
# 由 scripts/derive_resource_limits.sh 生成，勿手工编辑（改参数后重新运行）。
# 探测到的宿主机：${CORES} 核 / ${MEM_GB} GB
# 预算：CPU ${CPU_BUDGET_PCT}% = ${CPU_BUDGET} 核，内存 ${MEM_BUDGET_PCT}% = ${MEM_BUDGET} GB
#
# pids 为什么给这么多：容器 pids 计的是「任务数」= 进程 + 线程。一次文档解析会拉起
# MinerU（PyTorch 按 CPU 配额开线程）与 soffice（一棵进程树）。配额不足时表现是
# \`soffice: Cannot fork\` 与 \`asyncio to_thread: can't start new thread\`，
# 整个知识服务会连续报错到残留进程自行退出。
# 线程池上限不在这里配：代码读 cgroup 的 CPU 配额自动推导（parsers.py::_cpu_budget）。
KNOWLEDGE_API_CPU_LIMIT=${K_CPU}
KNOWLEDGE_API_MEMORY_LIMIT=${K_MEM}g
KNOWLEDGE_API_PIDS_LIMIT=${K_PIDS}
KNOWLEDGE_WORKER_CPU_LIMIT=${W_CPU}
KNOWLEDGE_WORKER_MEMORY_LIMIT=${W_MEM}g
KNOWLEDGE_WORKER_PIDS_LIMIT=${W_PIDS}
LIGHTRAG_CPU_LIMIT=${L_CPU}
LIGHTRAG_MEMORY_LIMIT=${L_MEM}g
LIGHTRAG_PIDS_LIMIT=${L_PIDS}
KNOWLEDGE_POSTGRES_CPU_LIMIT=${P_CPU}
KNOWLEDGE_POSTGRES_MEMORY_LIMIT=${P_MEM}g
KNOWLEDGE_POSTGRES_PIDS_LIMIT=${P_PIDS}
KNOWLEDGE_POSTGRES_MAX_CONNECTIONS=${PG_MAX_CONN}
$END_MARK
EOF
)

write_block() {
  local target=$1 block=$2
  # 用 -c 传脚本、heredoc 只喂受管区块内容：heredoc 与 herestring 不能同时占 stdin。
  python3 -c '
import sys
path, begin, end = sys.argv[1], sys.argv[2], sys.argv[3]
block = sys.stdin.read()
try:
    text = open(path, encoding="utf-8").read()
except FileNotFoundError:
    text = ""
out, skipping = [], False
for line in text.split("\n"):
    if line.strip() == begin:
        skipping = True
        continue
    if skipping:
        if line.strip() == end:
            skipping = False
        continue
    out.append(line)
while out and out[-1] == "":
    out.pop()
out.append("")
out.append(block.rstrip("\n"))
out.append("")
open(path, "w", encoding="utf-8").write("\n".join(out))
print("  wrote " + path)
' "$target" "$BEGIN_MARK" "$END_MARK" <<<"$block"
}

echo "宿主机：${CORES} 核 / ${MEM_GB} GB"
echo "预算  ：CPU ${CPU_BUDGET} 核（${CPU_BUDGET_PCT}%）/ 内存 ${MEM_BUDGET} GB（${MEM_BUDGET_PCT}%）"
echo
printf '  %-18s %6s %8s %8s\n' 服务 CPU 内存 pids
printf '  %-18s %6s %8s %8s\n' assistant "$A_CPU" "${A_MEM}g" "$A_PIDS"
printf '  %-18s %6s %8s %8s\n' knowledge-api "$K_CPU" "${K_MEM}g" "$K_PIDS"
printf '  %-18s %6s %8s %8s\n' knowledge-worker "$W_CPU" "${W_MEM}g" "$W_PIDS"
printf '  %-18s %6s %8s %8s\n' lightrag "$L_CPU" "${L_MEM}g" "$L_PIDS"
printf '  %-18s %6s %8s %8s\n' postgres "$P_CPU" "${P_MEM}g" "$P_PIDS"
echo
TOTAL_CPU=$(( A_CPU + K_CPU + W_CPU + L_CPU + P_CPU ))
TOTAL_MEM=$(( A_MEM + K_MEM + W_MEM + L_MEM + P_MEM ))
echo "  合计 CPU ${TOTAL_CPU} / ${CPU_BUDGET}，内存 ${TOTAL_MEM} / ${MEM_BUDGET} GB"
echo "  LLM 并发（主应用）: ${A_LLM}"
echo "  PostgreSQL max_connections: $PG_MAX_CONN"
echo

if [ "$DRY_RUN" = "1" ]; then
  echo "--dry-run：未写入。以下内容将写入两处 .env ——"
  echo
  echo "【deploy/.env】"
  printf '%s\n' "$ASSIST_BLOCK"
  echo
  echo "【knowledge-service/.env】"
  printf '%s\n' "$KNOWLEDGE_BLOCK"
  exit 0
fi

for dir in "deploy" "knowledge-service"; do
  target="${ROOT_DIR}/${dir}/.env"
  if [ ! -f "$target" ]; then
    echo "  跳过 ${target}（文件不存在；先按 .env.example 建好再运行本脚本）"
    continue
  fi
  if [ "$dir" = "deploy" ]; then
    write_block "$target" "$ASSIST_BLOCK"
  else
    write_block "$target" "$KNOWLEDGE_BLOCK"
  fi
done

echo
echo "完成。重启使上限生效："
echo "  docker compose -f deploy/docker-compose.code-mount.amd64.yml up -d --force-recreate"
echo "  docker compose -f knowledge-service/docker-compose.code-mount.amd64.yml up -d --force-recreate"
