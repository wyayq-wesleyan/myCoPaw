#!/usr/bin/env bash
# Build Docker image for the offline/internal edition.
# Run from repo root: bash scripts/docker_build.sh [IMAGE_TAG] [EXTRA_ARGS...]
# Example: bash scripts/docker_build.sh copaw:latest
#          bash scripts/docker_build.sh myreg/copaw:v1 --no-cache
#
# Build on top of a reusable base image to avoid repeated dependency downloads.
# Build the base first when needed:
#   bash scripts/docker_build_base.sh py312-base:1.0.1
#
# The app image now rebuilds the local console frontend first and copies the
# generated static assets into the Python package so the container always uses
# the current workspace UI.
#
# By default the Docker image excludes imessage (macOS-only).
# Override via:
#   COPAW_DISABLED_CHANNELS=imessage,voice bash scripts/docker_build.sh
#   COPAW_ENABLED_CHANNELS=discord,telegram  bash scripts/docker_build.sh
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DOCKERFILE="${DOCKERFILE:-$REPO_ROOT/deploy/Dockerfile}"
TAG="${1:-huizhi-assistant:2.0.1-arm64}"
shift || true
BASE_IMAGE="${BASE_IMAGE:-py312-base:2.0.2-amd64}"
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
DISABLED_CHANNELS="${QWENPAW_DISABLED_CHANNELS:-imessage}"

TMP_CONTEXT="$(mktemp -d)"
cleanup() {
    rm -rf "$TMP_CONTEXT"
}
trap cleanup EXIT

echo "[docker_build] Building console frontend..."
(cd console && npm run build)

mkdir -p "$TMP_CONTEXT/deploy"
cp "$DOCKERFILE" "$TMP_CONTEXT/deploy/Dockerfile"
cp pyproject.toml setup.py README.md "$TMP_CONTEXT/"
cp -R src "$TMP_CONTEXT/"
mkdir -p "$TMP_CONTEXT/plugins/bundle"
cp -R plugins/bundle/omp_workflows "$TMP_CONTEXT/plugins/bundle/"
mkdir -p "$TMP_CONTEXT/website/public"
cp -R website/public/docs "$TMP_CONTEXT/website/public/"
mkdir -p "$TMP_CONTEXT/src/qwenpaw/console"
cp -R console/dist/* "$TMP_CONTEXT/src/qwenpaw/console/"
cp -R deploy/config "$TMP_CONTEXT/deploy/"
cp deploy/entrypoint.sh "$TMP_CONTEXT/deploy/"

echo "[docker_build] Building image: $TAG (Dockerfile: $DOCKERFILE)"
docker build -f "$TMP_CONTEXT/deploy/Dockerfile" \
    "${PLATFORM_ARGS[@]}" \
    --build-arg BASE_IMAGE="$BASE_IMAGE" \
    --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
    --build-arg PIP_TRUSTED_HOST="$PIP_TRUSTED_HOST" \
    --build-arg QWENPAW_DISABLED_CHANNELS="$DISABLED_CHANNELS" \
    ${QWENPAW_ENABLED_CHANNELS:+--build-arg QWENPAW_ENABLED_CHANNELS="$QWENPAW_ENABLED_CHANNELS"} \
    -t "$TAG" "$@" "$TMP_CONTEXT"
echo "[docker_build] Done."
echo "[docker_build] CoPaw app port: 8088 (default). Override with -e COPAW_PORT=<port>."
echo "[docker_build] Base image: $BASE_IMAGE"
echo "[docker_build] Run: docker run -p 127.0.0.1:8088:8088 --add-host=host.docker.internal:host-gateway $TAG"
echo "[docker_build] Or:  docker run -e COPAW_PORT=3000 -p 127.0.0.1:3000:3000 $TAG"
echo "[docker_build] Host model services on the Docker host should use: http://host.docker.internal:<port>"
echo "[docker_build] Other machines on the same network should use their real IP/hostname, not 127.0.0.1."
