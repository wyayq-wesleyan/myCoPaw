#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-huizhi-knowledge:0.1.4-arm64}"
shift || true
BASE_IMAGE="${BASE_IMAGE:-py312-base:2.0.2-amd64}"
PLATFORM="${PLATFORM:-}"
ARGS=()
[[ -n "${PLATFORM}" ]] && ARGS+=(--platform "${PLATFORM}")

cd "${ROOT_DIR}/knowledge-service"
docker build -f Dockerfile "${ARGS[@]-}" \
  --build-arg BASE_IMAGE="${BASE_IMAGE}" \
  --build-arg INSTALL_KNOWLEDGE_EXTRAS="${INSTALL_KNOWLEDGE_EXTRAS:-0}" \
  --build-arg INSTALL_LIGHTRAG="${INSTALL_LIGHTRAG:-1}" \
  --build-arg PRELOAD_MINERU_MODELS="${PRELOAD_MINERU_MODELS:-0}" \
  -t "${TAG}" "$@" .
printf 'Knowledge base image built: %s\n' "${TAG}"
