#!/usr/bin/env bash
# 打出「全新 x86_64 服务器」用的全量离线部署包。
#
# 这是一个**装机包**，不是日常更新包：
#   * 含全部镜像（应用底座 + PostgreSQL/pgvector）
#   * 含主应用与知识服务代码、已构建的前端产物
#   * 含 .env 模板、compose、启动/自检脚本、部署 README
# 目标机器解压到 /data/huizhi-assistant 后，加载镜像 → 改 .env → 自检 → 启动即可。
#
# 与日常更新包的区别：
#   * 日常更新用 scripts/package_merge_updates.sh（只含代码与 deploy 层，不含镜像）
#   * 本脚本面向「一台什么都没有的新机器」，因此把镜像也一并打包（体积大得多）
#
# 用法：
#   bash deploy/full-offline/scripts/package_full_offline.sh [输出文件名]
#
# 前置条件：
#   1) 目标架构的底座镜像已构建并存在（默认 huizhi-runtime:<VERSION>-amd64）
#   2) 前端已构建（console/dist）——本脚本不替你构建，构建失败要能看见
#   3) pgvector 镜像已就位（默认 pgvector/pgvector:pg17，须是 amd64）
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC_DIR="${ROOT_DIR}/deploy/full-offline"
DIST_DIR="${ROOT_DIR}/dist/full-offline"

VERSION="${HUIZHI_VERSION:-2.2.1}"
ARCH="${HUIZHI_ARCH:-amd64}"
RUNTIME_IMAGE="${RUNTIME_IMAGE:-huizhi-runtime:${VERSION}-${ARCH}}"
PGVECTOR_IMAGE="${PGVECTOR_IMAGE:-pgvector/pgvector:pg17}"
PGVECTOR_ARCHIVE="${PGVECTOR_ARCHIVE:-pgvector-pg17-${ARCH}.tar.zst}"

STAMP="$(date +%Y%m%d)"
OUT_NAME="${1:-huizhi-assistant-full-${STAMP}.tar.gz}"
OUT_PATH="${DIST_DIR}/${OUT_NAME}"

STAGE="${DIST_DIR}/bundle"
APPDIR="${STAGE}/huizhi-assistant"

log() { echo "[full-offline] $*"; }
die() { echo "[full-offline] 错误：$*" >&2; exit 1; }

# 拷目录时排除 __pycache__ / .pyc：这些是构建机的字节码，跨机器可能对不上
# Python 版本，还可能盖住同名的 .py 源文件，属于纯噪音。
copy_code() {  # copy_code <源目录> <目标路径>
  rsync -a --exclude='__pycache__' --exclude='*.pyc' "$1" "$2"
}

# -------------------------------------------------------------------- 前置检查
log "检查前置条件…"

command -v docker >/dev/null 2>&1 || die "未找到 docker"
command -v rsync >/dev/null 2>&1 || die "未找到 rsync（打包需要，用于排除 __pycache__）"

docker image inspect "${RUNTIME_IMAGE}" >/dev/null 2>&1 \
  || die "镜像不存在：${RUNTIME_IMAGE}（先构建：bash scripts/docker_build_allinone.sh ${RUNTIME_IMAGE}）"
rt_arch="$(docker image inspect "${RUNTIME_IMAGE}" --format '{{.Architecture}}')"
[[ "${rt_arch}" == "${ARCH}" ]] \
  || die "${RUNTIME_IMAGE} 架构是 ${rt_arch}，期望 ${ARCH}"

docker image inspect "${PGVECTOR_IMAGE}" >/dev/null 2>&1 \
  || die "镜像不存在：${PGVECTOR_IMAGE}"
pg_arch="$(docker image inspect "${PGVECTOR_IMAGE}" --format '{{.Architecture}}')"
[[ "${pg_arch}" == "${ARCH}" ]] \
  || die "${PGVECTOR_IMAGE} 架构是 ${pg_arch}，期望 ${ARCH}。
       多架构镜像在本机默认取宿主架构；要取 ${ARCH} 请按 manifest 摘要拉取后重新 tag，
       或先用 docker tag 把 ${ARCH} 变体打上 ${PGVECTOR_IMAGE} 这个标签。"

[[ -f "${ROOT_DIR}/console/dist/index.html" ]] \
  || die "前端未构建：缺少 console/dist/index.html（先 cd console && npm run build）"

# -------------------------------------------------------------------- 组装目录
# 注意：app/ 与 scripts/ 不要预先 mkdir —— `cp -a 源目录 已存在的目标/` 会把源
# 目录整个塞进去（得到 app/app），知识服务就起不来了。让 cp 自己建这两层。
log "组装 ${APPDIR} …"
rm -rf "${STAGE}"
mkdir -p "${APPDIR}"/{images,scripts,deploy/config}
mkdir -p "${APPDIR}/app/console/dist"
mkdir -p "${APPDIR}/knowledge-service/initdb"

# --- 部署文档与配置模板（源：deploy/full-offline）
cp "${SRC_DIR}/README.md"                    "${APPDIR}/README.md"
cp "${SRC_DIR}/env/main.env.example"         "${APPDIR}/.env.example"
cp "${SRC_DIR}/env/main.env.example"         "${APPDIR}/.env"
cp "${SRC_DIR}/env/knowledge.env.example"    "${APPDIR}/knowledge-service/.env.example"
cp "${SRC_DIR}/env/knowledge.env.example"    "${APPDIR}/knowledge-service/.env"
cp "${SRC_DIR}/scripts/load-images.sh"       "${APPDIR}/scripts/"
cp "${SRC_DIR}/scripts/preflight-check.sh"   "${APPDIR}/scripts/"
cp "${SRC_DIR}/scripts/start-all.sh"         "${APPDIR}/scripts/"
cp "${SRC_DIR}/scripts/stop-all.sh"          "${APPDIR}/scripts/"
cp "${SRC_DIR}/config/01-create-graph-database.sql" \
                                             "${APPDIR}/knowledge-service/initdb/"

# --- compose 与容器入口（源：仓库既有文件，amd64 版本）
cp "${ROOT_DIR}/deploy/docker-compose.code-mount.${ARCH}.yml" "${APPDIR}/docker-compose.yml"
cp "${ROOT_DIR}/deploy/entrypoint.sh"                         "${APPDIR}/entrypoint.sh"
cp "${ROOT_DIR}/deploy/code-mount-entrypoint.sh"              "${APPDIR}/code-mount-entrypoint.sh"
cp "${ROOT_DIR}/deploy/config/supervisord.conf.template"      "${APPDIR}/deploy/config/"
cp "${ROOT_DIR}/knowledge-service/docker-compose.code-mount.${ARCH}.yml" \
                                                              "${APPDIR}/knowledge-service/docker-compose.yml"
cp "${ROOT_DIR}/knowledge-service/docker-compose.postgres.yml" \
                                                              "${APPDIR}/knowledge-service/docker-compose.postgres.yml"

# compose 里引用的镜像名必须与本包实际提供的镜像一致
if [[ "${RUNTIME_IMAGE}" != "huizhi-runtime:${VERSION}-${ARCH}" ]]; then
  log "把 compose 默认镜像名改写为 ${RUNTIME_IMAGE}"
  for f in "${APPDIR}/docker-compose.yml" \
           "${APPDIR}/knowledge-service/docker-compose.yml"; do
    sed -i.bak -E "s#huizhi-runtime:${VERSION}-${ARCH}#${RUNTIME_IMAGE}#g" "$f"
    rm -f "$f.bak"
  done
  sed -i.bak -E "s#^MYCOPAW_IMAGE=.*#MYCOPAW_IMAGE=${RUNTIME_IMAGE}#" \
    "${APPDIR}/.env" "${APPDIR}/.env.example"
  rm -f "${APPDIR}/.env.bak" "${APPDIR}/.env.example.bak"
fi

# --- 应用代码 + 已构建前端
log "复制主应用代码与前端产物…"
copy_code "${ROOT_DIR}/src/."            "${APPDIR}/app/src/"
copy_code "${ROOT_DIR}/console/dist/."   "${APPDIR}/app/console/dist/"
for f in pyproject.toml setup.py README.md README_zh.md; do
  [[ -e "${ROOT_DIR}/${f}" ]] && cp -a "${ROOT_DIR}/${f}" "${APPDIR}/app/" || true
done
# plugins 若存在一并带上（部分账号功能依赖）
[[ -d "${ROOT_DIR}/plugins" ]] && copy_code "${ROOT_DIR}/plugins/." "${APPDIR}/app/plugins/" || true

log "复制知识服务代码…"
copy_code "${ROOT_DIR}/knowledge-service/app/."     "${APPDIR}/knowledge-service/app/"
copy_code "${ROOT_DIR}/knowledge-service/scripts/." "${APPDIR}/knowledge-service/scripts/"
for f in pyproject.toml README.md; do
  [[ -e "${ROOT_DIR}/knowledge-service/${f}" ]] \
    && cp -a "${ROOT_DIR}/knowledge-service/${f}" "${APPDIR}/knowledge-service/" || true
done

# --- 镜像
log "导出镜像（这一步最慢：底座镜像约 20GB）…"
RUNTIME_ARCHIVE="huizhi-runtime-${VERSION}-${ARCH}.tar.zst"
if [[ -f "${DIST_DIR}/${RUNTIME_ARCHIVE}" ]]; then
  log "复用已有 ${RUNTIME_ARCHIVE}（如需重新导出先删除它）"
else
  COMPRESS=zstd bash "${ROOT_DIR}/scripts/docker_export_image.sh" \
    "${RUNTIME_IMAGE}" "${DIST_DIR}/${RUNTIME_ARCHIVE}"
fi
cp "${DIST_DIR}/${RUNTIME_ARCHIVE}" "${APPDIR}/images/"

if [[ -f "${DIST_DIR}/${PGVECTOR_ARCHIVE}" ]]; then
  log "复用已有 ${PGVECTOR_ARCHIVE}"
else
  COMPRESS=zstd bash "${ROOT_DIR}/scripts/docker_export_image.sh" \
    "${PGVECTOR_IMAGE}" "${DIST_DIR}/${PGVECTOR_ARCHIVE}"
fi
cp "${DIST_DIR}/${PGVECTOR_ARCHIVE}" "${APPDIR}/images/"

# 校验文件统一用「文件名」，这样在目标机器 images/ 里直接 -c 即可
( cd "${APPDIR}/images"
  for f in *.tar.zst; do shasum -a 256 "$f" > "$f.sha256"; done )

# -------------------------------------------------------------------- 收尾检查
log "包内自检…"
for must in README.md .env .env.example docker-compose.yml \
            knowledge-service/.env knowledge-service/docker-compose.yml \
            knowledge-service/docker-compose.postgres.yml \
            knowledge-service/initdb/01-create-graph-database.sql \
            app/src/qwenpaw app/console/dist/index.html \
            knowledge-service/app \
            images/${RUNTIME_ARCHIVE} images/${PGVECTOR_ARCHIVE}; do
  [[ -e "${APPDIR}/${must}" ]] || die "包内缺少必需文件：${must}"
done
log "必需文件齐全"

# 结构断言：compose 把 ./app 挂在容器的 /opt/huizhi-knowledge/app，容器的
# PYTHONPATH 也是 /opt/huizhi-knowledge，因此包内必须是 knowledge-service/app/main.py。
# 一旦目录多套一层（app/app），知识服务导入就会失败 —— 这里直接挡住。
[[ -f "${APPDIR}/knowledge-service/app/main.py" ]] \
  || die "知识服务代码层级不对：期望 knowledge-service/app/main.py，实际不存在"
[[ -f "${APPDIR}/app/src/qwenpaw/app/__init__.py" ]] \
  || die "主应用代码层级不对：期望 app/src/qwenpaw/…"
for d in "${APPDIR}/knowledge-service/app/app" "${APPDIR}/knowledge-service/scripts/scripts"; do
  [[ -e "${d}" ]] && die "目录被多套了一层：${d#${APPDIR}/}（cp 目标已存在目录所致）"
done
if find "${APPDIR}/app" "${APPDIR}/knowledge-service" -name '__pycache__' | grep -q .; then
  die "包内混入 __pycache__（构建机字节码不应随包分发）"
fi
log "目录结构断言通过"

# 若本机可解析 compose，顺带验证三个 compose 语法（解析失败直接中止打包）
if docker compose version >/dev/null 2>&1; then
  ( cd "${APPDIR}" && docker compose -f docker-compose.yml config >/dev/null ) \
    || die "主应用 compose 解析失败"
  ( cd "${APPDIR}/knowledge-service" && docker compose -f docker-compose.yml config >/dev/null ) \
    || die "知识服务 compose 解析失败"
  ( cd "${APPDIR}/knowledge-service" && docker compose -f docker-compose.postgres.yml config >/dev/null ) \
    || die "PostgreSQL compose 解析失败"
  log "三个 compose 解析正常"
fi

# -------------------------------------------------------------------- 打包
log "生成压缩包…"
mkdir -p "${DIST_DIR}"
tar -czf "${OUT_PATH}" -C "${STAGE}" huizhi-assistant
# 校验文件里写「文件名」而不是绝对路径：这样把 .tar.gz 与 .sha256 一起拷到目标机器后，
# 在该目录直接 `shasum -a 256 -c <file>.sha256` 就能校验（写构建机绝对路径会失效）。
OUT_BASE="$(basename "${OUT_PATH}")"
( cd "${DIST_DIR}" && shasum -a 256 "${OUT_BASE}" > "${OUT_BASE}.sha256" )

# -------------------------------------------------------------------- 分卷
# 完整包约 6.6GB，超过常见传输通道/网盘 1GB 的单文件上限，因此默认切成 900MiB 分卷。
# 目标机器上用 join-parts.sh 合并回原包，合并结果与原始包逐字节一致（sha256 相同）。
PART_SIZE="${HUIZHI_PART_SIZE:-900m}"
PARTS_DIR="${DIST_DIR}/parts"
if [[ "${PART_SIZE}" == "none" ]]; then
  log "跳过分卷（HUIZHI_PART_SIZE=none）"
  PART_COUNT=0
else
  log "切分分卷（每卷 ${PART_SIZE}）…"
  rm -rf "${PARTS_DIR}"
  mkdir -p "${PARTS_DIR}"
  ( cd "${DIST_DIR}" && split -b "${PART_SIZE}" -a 2 -d "${OUT_BASE}" "${PARTS_DIR}/${OUT_BASE}.part-" )
  # sha256 与合并脚本一起放进 parts/，目标机器在该目录直接合并+校验
  cp "${OUT_PATH}.sha256" "${PARTS_DIR}/"
  cp "${SRC_DIR}/scripts/join-parts.sh" "${PARTS_DIR}/"
  chmod +x "${PARTS_DIR}/join-parts.sh"
  PART_COUNT="$(ls -1 "${PARTS_DIR}/${OUT_BASE}".part-* | wc -l | tr -d ' ')"
  # 合并结果必须与原包一致，在出包处就验证一次，避免把坏分卷发出去
  if [[ "$(cat "${PARTS_DIR}/${OUT_BASE}".part-* | shasum -a 256 | cut -d' ' -f1)" \
        != "$(cut -d' ' -f1 < "${OUT_PATH}.sha256")" ]]; then
    die "分卷合并后 sha256 与原包不一致 —— 分卷不可用"
  fi
  log "分卷完成：${PART_COUNT} 个，合并校验通过"
fi

SIZE="$(du -h "${OUT_PATH}" | cut -f1)"
cat > "${OUT_PATH}.absolute-paths.txt" <<EOF
$(cd "$(dirname "${OUT_PATH}")" && pwd)/$(basename "${OUT_PATH}")
$(cd "$(dirname "${OUT_PATH}")" && pwd)/$(basename "${OUT_PATH}").sha256
EOF

echo
log "完成：${OUT_PATH} (${SIZE})"
log "校验：${OUT_PATH}.sha256"
echo
if [[ "${PART_COUNT}" -gt 0 ]]; then
  echo "分卷（每个 <1GB，逐个传输；目标机器上用 join-parts.sh 合并）："
  ( cd "${PARTS_DIR}" && ls -1 | sed "s#^#  $(pwd)/#" )
  echo
  echo "目标机器上："
  echo "   # 1) 把 parts/ 下所有分卷 + join-parts.sh + .sha256 传到同一目录"
  echo "   bash join-parts.sh $(basename "${OUT_PATH}")"
  echo "   cd /data && tar -xf $(basename "${OUT_PATH}")"
  echo "   cd /data/huizhi-assistant && bash scripts/load-images.sh"
else
  echo "产物绝对路径（可直接 scp）："
  cat "${OUT_PATH}.absolute-paths.txt"
  echo
  echo "目标机器上："
  echo "  cd /data && tar -xzf $(basename "${OUT_PATH}")"
  echo "  cd /data/huizhi-assistant && bash scripts/load-images.sh"
fi
