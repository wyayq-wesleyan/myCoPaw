#!/usr/bin/env bash
# Build both arm64 and amd64 base/app images.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:-2.0.0}"
BASE_VERSION="${BASE_VERSION:-1.0.0}"

build_one() {
  local arch="$1"
  local platform="$2"
  local base_tag="py311-base:${BASE_VERSION}-${arch}"
  local app_tag="mycopaw-offline:${VERSION}-${arch}"

  echo "[docker_build_matrix] Building base image for ${arch}"
  PLATFORM="${platform}" bash scripts/docker_build_base.sh "${base_tag}"

  echo "[docker_build_matrix] Building app image for ${arch}"
  PLATFORM="${platform}" BASE_IMAGE="${base_tag}" bash scripts/docker_build.sh "${app_tag}"
}

build_one arm64 linux/arm64
build_one amd64 linux/amd64

echo "[docker_build_matrix] Done."
