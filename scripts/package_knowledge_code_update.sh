#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist/docker-images"
PACKAGE_NAME="${1:-huizhi-knowledge-code-$(date +%Y%m%d).tar.gz}"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "${STAGE_DIR}"' EXIT

mkdir -p "${DIST_DIR}" "${STAGE_DIR}/knowledge-service"
cp -R "${ROOT_DIR}/knowledge-service/app" "${STAGE_DIR}/knowledge-service/"
cp -R "${ROOT_DIR}/knowledge-service/scripts" "${STAGE_DIR}/knowledge-service/"
cp "${ROOT_DIR}/knowledge-service/pyproject.toml" "${ROOT_DIR}/knowledge-service/README.md" "${STAGE_DIR}/knowledge-service/"
# Compose files contain only resource and service wiring, no secrets or data;
# include them so production code-mount updates carry the tuned multi-user
# CPU/memory profile as well.
cp "${ROOT_DIR}/knowledge-service/docker-compose.code-mount.arm64.yml" \
   "${ROOT_DIR}/knowledge-service/docker-compose.postgres.yml" \
   "${STAGE_DIR}/knowledge-service/"
find "${STAGE_DIR}" \( -name __pycache__ -o -name '*.pyc' -o -name '*.pyo' -o -name .DS_Store \) -prune -exec rm -rf {} +

tar -C "${STAGE_DIR}" -czf "${DIST_DIR}/${PACKAGE_NAME}" knowledge-service
shasum -a 256 "${DIST_DIR}/${PACKAGE_NAME}" > "${DIST_DIR}/${PACKAGE_NAME}.sha256"
printf 'Knowledge code update package: %s\nChecksum: %s\n' \
  "${DIST_DIR}/${PACKAGE_NAME}" "${DIST_DIR}/${PACKAGE_NAME}.sha256"
