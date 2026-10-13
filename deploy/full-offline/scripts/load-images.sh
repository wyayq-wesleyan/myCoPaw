#!/usr/bin/env bash
# 加载本目录 images/ 下的全部镜像到本机 Docker。
#
# 幂等：镜像已存在时 docker load 会直接跳过，重复执行安全。
# 需要 zstd（镜像用 zstd 压缩：约 20GB 的镜像压到 6.4GB，gzip 在此体积下又慢又不划算）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGES_DIR="${HERE}/../images"

if [[ ! -d "${IMAGES_DIR}" ]]; then
  echo "找不到镜像目录：${IMAGES_DIR}" >&2
  exit 1
fi

if ! command -v zstd >/dev/null 2>&1; then
  echo "缺少 zstd 解压工具。" >&2
  echo "  Debian/Ubuntu:  apt-get install -y zstd" >&2
  echo "  RHEL/CentOS:    yum install -y zstd" >&2
  echo "（镜像也可以用 gzip/none 重新导出，但那需要改本脚本与导出流程。）" >&2
  exit 1
fi

shopt -s nullglob
archives=("${IMAGES_DIR}"/*.tar.zst)
if [[ ${#archives[@]} -eq 0 ]]; then
  echo "images/ 下没有 .tar.zst 镜像归档。" >&2
  exit 1
fi

for archive in "${archives[@]}"; do
  name="$(basename "${archive}")"
  echo "==> 校验 ${name}"
  if [[ -f "${archive}.sha256" ]]; then
    (cd "${IMAGES_DIR}" && shasum -a 256 -c "${name}.sha256")
  else
    echo "    （没有 .sha256，跳过校验）"
  fi

  echo "==> 加载 ${name}（大文件，请耐心等待）"
  zstd -dc "${archive}" | docker load
done

echo
echo "==> 已加载的镜像："
docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' \
  | grep -E 'huizhi-runtime|pgvector' || true
echo
echo "下一步：编辑 .env 与 knowledge-service/.env，然后按 README 启动。"
