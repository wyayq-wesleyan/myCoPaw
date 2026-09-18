#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist/docker-images"
PACKAGE_NAME="${1:-huizhi-assistant-code-$(date +%Y%m%d).tar.gz}"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "${STAGE_DIR}"' EXIT

mkdir -p "${DIST_DIR}" "${STAGE_DIR}"

# Reuse the canonical app packager so the frontend is rebuilt and the app
# payload stays identical to the regular code-update package.
bash "${ROOT_DIR}/scripts/package_code_update.sh" "huizhi-assistant-code-staging.tar.gz" >/dev/null
tar -xzf "${DIST_DIR}/huizhi-assistant-code-staging.tar.gz" -C "${STAGE_DIR}"
rm -f "${DIST_DIR}/huizhi-assistant-code-staging.tar.gz" "${DIST_DIR}/huizhi-assistant-code-staging.tar.gz.sha256"

# Include the small deployment-layer logging fix in the same archive.
mkdir -p "${STAGE_DIR}/deploy/config"
cp "${ROOT_DIR}/deploy/code-mount-entrypoint.sh" "${STAGE_DIR}/deploy/"
cp "${ROOT_DIR}/deploy/docker-compose.code-mount.arm64.yml" "${STAGE_DIR}/deploy/"
cp "${ROOT_DIR}/deploy/entrypoint.sh" "${STAGE_DIR}/deploy/"
cp "${ROOT_DIR}/deploy/config/supervisord.conf.template" "${STAGE_DIR}/deploy/config/"

tar -C "${STAGE_DIR}" -czf "${DIST_DIR}/${PACKAGE_NAME}" app deploy
shasum -a 256 "${DIST_DIR}/${PACKAGE_NAME}" > "${DIST_DIR}/${PACKAGE_NAME}.sha256"
printf 'Assistant combined update package: %s\nChecksum: %s\n' \
  "${DIST_DIR}/${PACKAGE_NAME}" "${DIST_DIR}/${PACKAGE_NAME}.sha256"
