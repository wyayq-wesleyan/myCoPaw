#!/usr/bin/env bash
# Build Docker image (includes console frontend build in multi-stage).
# Run from repo root: bash scripts/docker_build.sh [IMAGE_TAG] [EXTRA_ARGS...]
# Example: bash scripts/docker_build.sh copaw:latest
#          bash scripts/docker_build.sh myreg/copaw:v1 --no-cache
#
# Build on top of a reusable base image to avoid repeated dependency downloads.
# Build the base first when needed:
#   bash scripts/docker_build_base.sh py311-base:1.0.0
#
# By default the Docker image excludes imessage (macOS-only).
# Override via:
#   COPAW_DISABLED_CHANNELS=imessage,voice bash scripts/docker_build.sh
#   COPAW_ENABLED_CHANNELS=discord,telegram  bash scripts/docker_build.sh
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DOCKERFILE="${DOCKERFILE:-$REPO_ROOT/deploy/Dockerfile}"
TAG="${1:-copaw:latest}"
shift || true
BASE_IMAGE="${BASE_IMAGE:-py311-base:1.0.0}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-pypi.tuna.tsinghua.edu.cn}"
PLATFORM="${PLATFORM:-}"

if [[ -n "$PLATFORM" ]]; then
    PLATFORM_ARGS=(--platform "$PLATFORM")
else
    PLATFORM_ARGS=()
fi

# Channels to exclude from the image (default: imessage).
DISABLED_CHANNELS="${COPAW_DISABLED_CHANNELS:-imessage}"

echo "[docker_build] Building image: $TAG (Dockerfile: $DOCKERFILE)"
docker build -f "$DOCKERFILE" \
    "${PLATFORM_ARGS[@]}" \
    --build-arg BASE_IMAGE="$BASE_IMAGE" \
    --build-arg NPM_REGISTRY="$NPM_REGISTRY" \
    --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
    --build-arg PIP_TRUSTED_HOST="$PIP_TRUSTED_HOST" \
    --build-arg COPAW_DISABLED_CHANNELS="$DISABLED_CHANNELS" \
    ${COPAW_ENABLED_CHANNELS:+--build-arg COPAW_ENABLED_CHANNELS="$COPAW_ENABLED_CHANNELS"} \
    -t "$TAG" "$@" .
echo "[docker_build] Done."
echo "[docker_build] CoPaw app port: 8088 (default). Override with -e COPAW_PORT=<port>."
echo "[docker_build] Base image: $BASE_IMAGE"
echo "[docker_build] Run: docker run -p 127.0.0.1:8088:8088 $TAG"
echo "[docker_build] Or:  docker run -e COPAW_PORT=3000 -p 127.0.0.1:3000:3000 $TAG"
