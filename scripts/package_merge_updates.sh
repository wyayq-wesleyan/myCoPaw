#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist/docker-images"
TODAY="$(date +%Y%m%d)"
ASSISTANT_PACKAGE="${1:-${DIST_DIR}/huizhi-assistant-app-code-${TODAY}.tar.gz}"
KNOWLEDGE_PACKAGE="${2:-${DIST_DIR}/huizhi-knowledge-code-${TODAY}.tar.gz}"
PACKAGE_NAME="${3:-huizhi-code-update-${TODAY}.tar.gz}"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "${STAGE_DIR}"' EXIT

mkdir -p "${DIST_DIR}" "${STAGE_DIR}/deploy/config"
[[ -f "${ASSISTANT_PACKAGE}" ]] || { echo "Missing assistant package: ${ASSISTANT_PACKAGE}" >&2; exit 1; }
[[ -f "${KNOWLEDGE_PACKAGE}" ]] || { echo "Missing knowledge package: ${KNOWLEDGE_PACKAGE}" >&2; exit 1; }

# Both source packages are code-only archives. Extract them under one staging
# directory while keeping their existing top-level app/ and knowledge-service/
# layout. No images, .env files, or runtime data are added.
tar -xzf "${ASSISTANT_PACKAGE}" -C "${STAGE_DIR}"
tar -xzf "${KNOWLEDGE_PACKAGE}" -C "${STAGE_DIR}"
find "${STAGE_DIR}" \
  \( -name __pycache__ -o -name '*.pyc' -o -name '*.pyo' -o -name .DS_Store \) \
  -prune -exec rm -rf {} +

# The merged code packages are enough for Python/frontend changes. Include
# the small deployment layer needed by the current logging/schema fix so a
# single upload also enables host-mounted logs.
cp "${ROOT_DIR}/deploy/code-mount-entrypoint.sh" "${STAGE_DIR}/deploy/"
cp "${ROOT_DIR}/deploy/docker-compose.code-mount.arm64.yml" "${STAGE_DIR}/deploy/"
cp "${ROOT_DIR}/deploy/entrypoint.sh" "${STAGE_DIR}/deploy/"
cp "${ROOT_DIR}/deploy/config/supervisord.conf.template" "${STAGE_DIR}/deploy/config/"

tar -C "${STAGE_DIR}" -czf "${DIST_DIR}/${PACKAGE_NAME}" app knowledge-service deploy
(cd "${DIST_DIR}" && shasum -a 256 "${PACKAGE_NAME}") > "${DIST_DIR}/${PACKAGE_NAME}.sha256"
printf 'Merged code update package: %s\nChecksum: %s\n' \
  "${DIST_DIR}/${PACKAGE_NAME}" "${DIST_DIR}/${PACKAGE_NAME}.sha256"
