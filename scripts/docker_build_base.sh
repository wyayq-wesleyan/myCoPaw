#!/usr/bin/env bash
# Build the reusable offline-friendly Python runtime for QwenPaw and internal apps.
# Run from repo root: bash scripts/docker_build_base.sh [IMAGE_TAG] [EXTRA_ARGS...]
#
# Examples:
#   bash scripts/docker_build_base.sh py311-base:1.0.1
#   bash scripts/docker_build_base.sh registry.local/py311-base:1.0.1 --no-cache
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DOCKERFILE="${DOCKERFILE:-$REPO_ROOT/deploy/Dockerfile.base}"
TAG="${1:-py311-base:2.0.1-arm64}"
shift || true

PYTHON_IMAGE="${PYTHON_IMAGE:-m.daocloud.io/docker.io/library/python:3.11-slim-bookworm}"
APT_MIRROR="${APT_MIRROR:-mirrors.aliyun.com}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-pypi.tuna.tsinghua.edu.cn}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"
PLATFORM="${PLATFORM:-}"

if [[ -n "$PLATFORM" ]]; then
    PLATFORM_ARGS=(--platform "$PLATFORM")
else
    PLATFORM_ARGS=()
fi

TMP_CONTEXT="$(mktemp -d)"
cleanup() {
    rm -rf "$TMP_CONTEXT"
}
trap cleanup EXIT

mkdir -p "$TMP_CONTEXT/deploy"
cp "$DOCKERFILE" "$TMP_CONTEXT/deploy/Dockerfile.base"
cp -R deploy/offline-assets "$TMP_CONTEXT/deploy/"

echo "[docker_build_base] Building image: $TAG (Dockerfile: $DOCKERFILE)"
docker build -f "$TMP_CONTEXT/deploy/Dockerfile.base" \
    "${PLATFORM_ARGS[@]}" \
    --build-arg PYTHON_IMAGE="$PYTHON_IMAGE" \
    --build-arg APT_MIRROR="$APT_MIRROR" \
    --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
    --build-arg PIP_TRUSTED_HOST="$PIP_TRUSTED_HOST" \
    --build-arg NPM_REGISTRY="$NPM_REGISTRY" \
    -t "$TAG" "$@" "$TMP_CONTEXT"
echo "[docker_build_base] Done."
echo "[docker_build_base] Base image tag: $TAG"
echo "[docker_build_base] Python image: $PYTHON_IMAGE"
echo "[docker_build_base] Offline assets root: deploy/offline-assets/<arch>/"
