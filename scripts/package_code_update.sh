#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist/docker-images"
APP_DIR="${DIST_DIR}/app"
PACKAGE_NAME="${1:-huizhi-assistant-app-code-2.0.1.tar.gz}"

cd "${ROOT_DIR}"

npm -C console run -s build

rm -rf "${APP_DIR}"
mkdir -p "${APP_DIR}/console"
mkdir -p "${APP_DIR}/plugins/bundle"

cp -R src "${APP_DIR}/src"
cp -R console/dist "${APP_DIR}/console/dist"
cp -R plugins/bundle/omp_workflows "${APP_DIR}/plugins/bundle/"

# Keep the packaged Python fallback console assets in sync with the latest
# frontend build so source/code-mount deployments never serve stale bundles.
rm -rf "${APP_DIR}/src/qwenpaw/console"
mkdir -p "${APP_DIR}/src/qwenpaw/console"
cp -R console/dist/. "${APP_DIR}/src/qwenpaw/console/"

cp pyproject.toml setup.py README.md README_zh.md "${APP_DIR}/"

find "${APP_DIR}" \
  \( -name "__pycache__" -o -name "*.pyc" -o -name "*.pyo" -o -name ".DS_Store" \) \
  -prune -exec rm -rf {} +
rm -rf "${APP_DIR}/src/copaw.egg-info"

tar -C "${DIST_DIR}" -czf "${DIST_DIR}/${PACKAGE_NAME}" app
shasum -a 256 "${DIST_DIR}/${PACKAGE_NAME}" > "${DIST_DIR}/${PACKAGE_NAME}.sha256"

echo "Code update package: ${DIST_DIR}/${PACKAGE_NAME}"
echo "Checksum: ${DIST_DIR}/${PACKAGE_NAME}.sha256"
