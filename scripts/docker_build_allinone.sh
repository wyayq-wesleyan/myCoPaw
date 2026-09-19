#!/usr/bin/env bash
# Build the single all-in-one runtime image used by every service.
#
#   assistant     /entrypoint.sh            (supervisor, 8088)
#   knowledge-api uvicorn app.main:app      (18100)
#   knowledge-worker python -m app.worker
#   lightrag      python -m lightrag...     (9621)
#
# One image instead of one per service keeps the shared dependency layers from
# being duplicated in the exported archive. Code is bind-mounted at deploy
# time, so this image is the dependency layer only.
#
# Usage:
#   bash scripts/docker_build_allinone.sh [TAG] [EXTRA_ARGS...]
#
# Examples:
#   bash scripts/docker_build_allinone.sh huizhi-runtime:2.2.1-arm64
#   PLATFORM=linux/amd64 bash scripts/docker_build_allinone.sh huizhi-runtime:2.2.1-amd64
#
# Cross-building amd64 on an arm64 host runs every install step through QEMU
# and takes hours; prefer building on a matching host when one is available.
# Set SKIP_MINERU=1 for a fast dependency smoke build without torch/MinerU.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

TAG="${1:-huizhi-runtime:2.2.1-arm64}"
shift || true

# Derive the base image from the target architecture. The base image is built
# per architecture and is not multi-arch, so reusing the arm64 tag for an amd64
# build fails with "no match for platform in manifest".
TAG_ARCH="${TAG##*-}"
case "${TAG_ARCH}" in
  amd64|x86_64) DEFAULT_BASE_ARCH="amd64" ;;
  *)            DEFAULT_BASE_ARCH="arm64" ;;
esac
if [[ -n "${PLATFORM:-}" && "${PLATFORM}" == *amd64* ]]; then
  DEFAULT_BASE_ARCH="amd64"
elif [[ -n "${PLATFORM:-}" && "${PLATFORM}" == *arm64* ]]; then
  DEFAULT_BASE_ARCH="arm64"
fi

BASE_IMAGE="${BASE_IMAGE:-py312-base:2.0.2-${DEFAULT_BASE_ARCH}}"
PLATFORM="${PLATFORM:-}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-pypi.tuna.tsinghua.edu.cn}"
# CPU-only torch keeps ~4.4GB of CUDA runtime out of an image with no GPU.
# The aliyun mirror is a flat wheel directory consumed by exact URL (it is
# not a PEP 503 index, and --find-links over it is prohibitively slow).
TORCH_BASE_URL="${TORCH_BASE_URL:-https://mirrors.aliyun.com/pytorch-wheels/cpu}"
INSTALL_KNOWLEDGE_EXTRAS="${INSTALL_KNOWLEDGE_EXTRAS:-1}"
PRELOAD_MINERU_MODELS="${PRELOAD_MINERU_MODELS:-1}"
[[ "${SKIP_MINERU:-0}" == "1" ]] && {
  INSTALL_KNOWLEDGE_EXTRAS=0
  PRELOAD_MINERU_MODELS=0
}

# A scalar rather than an array: macOS ships bash 3.2, where expanding an
# empty array under `set -u` aborts with "unbound variable".
PLATFORM_ARG=""
[[ -n "${PLATFORM}" ]] && PLATFORM_ARG="--platform=${PLATFORM}"

# Guard against a tag/architecture mismatch, the same way the base builder does.
HOST_ARCH="$(uname -m)"
if [[ "${TAG}" == *-amd64 && -z "${PLATFORM}" && "${HOST_ARCH}" != "x86_64" ]]; then
  echo "[allinone] WARNING: tag '${TAG}' says amd64 but host is ${HOST_ARCH}."
  echo "[allinone] Cross-building requires: PLATFORM=linux/amd64 $0 ${TAG}"
elif [[ "${TAG}" == *-arm64 && -z "${PLATFORM}" && "${HOST_ARCH}" != "arm64" ]]; then
  echo "[allinone] WARNING: tag '${TAG}' says arm64 but host is ${HOST_ARCH}."
fi

echo "[allinone] Building console frontend..."
(cd console && npm run build)

# Assemble a minimal build context. The frontend is built first so its output
# can be copied in; the runtime itself is installed from the packages.
CONTEXT_DIR="$(mktemp -d)"
trap 'rm -rf "${CONTEXT_DIR}"' EXIT

mkdir -p "${CONTEXT_DIR}/deploy/config"
mkdir -p "${CONTEXT_DIR}/app" "${CONTEXT_DIR}/knowledge-service"

cp pyproject.toml setup.py README.md "${CONTEXT_DIR}/app/"
cp -R src "${CONTEXT_DIR}/app/src"
mkdir -p "${CONTEXT_DIR}/app/plugins/bundle" "${CONTEXT_DIR}/app/website/public" "${CONTEXT_DIR}/app/console"
cp -R plugins/bundle/omp_workflows "${CONTEXT_DIR}/app/plugins/bundle/"
cp -R website/public/docs "${CONTEXT_DIR}/app/website/public/"
cp -R console/dist "${CONTEXT_DIR}/app/console/dist"

cp knowledge-service/pyproject.toml knowledge-service/README.md "${CONTEXT_DIR}/knowledge-service/"
cp -R knowledge-service/app "${CONTEXT_DIR}/knowledge-service/app"
cp -R knowledge-service/scripts "${CONTEXT_DIR}/knowledge-service/scripts"

cp deploy/config/supervisord.conf.template "${CONTEXT_DIR}/deploy/config/"
cp deploy/entrypoint.sh "${CONTEXT_DIR}/deploy/"
cp deploy/Dockerfile.allinone "${CONTEXT_DIR}/deploy/Dockerfile.allinone"

echo "[allinone] Building image: ${TAG}"
echo "[allinone] Base image: ${BASE_IMAGE}"
echo "[allinone] MinerU extras: ${INSTALL_KNOWLEDGE_EXTRAS} (preload models: ${PRELOAD_MINERU_MODELS})"

docker build \
  -f "${CONTEXT_DIR}/deploy/Dockerfile.allinone" \
  ${PLATFORM_ARG} \
  --build-arg BASE_IMAGE="${BASE_IMAGE}" \
  --build-arg PIP_INDEX_URL="${PIP_INDEX_URL}" \
  --build-arg PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST}" \
  --build-arg TORCH_BASE_URL="${TORCH_BASE_URL}" \
  --build-arg INSTALL_KNOWLEDGE_EXTRAS="${INSTALL_KNOWLEDGE_EXTRAS}" \
  --build-arg INSTALL_LIGHTRAG="${INSTALL_LIGHTRAG:-1}" \
  --build-arg PRELOAD_MINERU_MODELS="${PRELOAD_MINERU_MODELS}" \
  -t "${TAG}" "$@" "${CONTEXT_DIR}"

echo "[allinone] Done."
echo "[allinone] Image: ${TAG}"
docker images "${TAG%%:*}" --format '  {{.Repository}}:{{.Tag}}  {{.Size}}'
