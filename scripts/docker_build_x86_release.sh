#!/usr/bin/env bash
# Build x86_64/amd64 offline images for server delivery.
# Run from repo root:
#   bash scripts/docker_build_x86_release.sh [APP_VERSION]
#
# Examples:
#   bash scripts/docker_build_x86_release.sh 2.0.0
#   BASE_VERSION=1.0.1 bash scripts/docker_build_x86_release.sh 2.0.0
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

APP_VERSION="${1:-2.0.0}"
BASE_VERSION="${BASE_VERSION:-1.0.0}"

BASE_TAG="py311-base:${BASE_VERSION}-amd64"
APP_TAG="mycopaw-offline:${APP_VERSION}-amd64"

echo "[docker_build_x86_release] Building x86_64 base image: ${BASE_TAG}"
PLATFORM=linux/amd64 bash scripts/docker_build_base.sh "${BASE_TAG}"

echo "[docker_build_x86_release] Building x86_64 app image: ${APP_TAG}"
PLATFORM=linux/amd64 BASE_IMAGE="${BASE_TAG}" bash scripts/docker_build.sh "${APP_TAG}"

echo "[docker_build_x86_release] Done."
echo "[docker_build_x86_release] Base image: ${BASE_TAG}"
echo "[docker_build_x86_release] App image:  ${APP_TAG}"
