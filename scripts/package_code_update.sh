#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist/docker-images"
APP_DIR="${DIST_DIR}/app"
PACKAGE_NAME="${1:-huizhi-assistant-app-code-$(date +%Y%m%d).tar.gz}"

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
# 运维脚本随包：资源上限推导按宿主机容量生成 .env 区块，没有它就只能手算。
mkdir -p "${APP_DIR}/scripts"
cp scripts/derive_resource_limits.sh "${APP_DIR}/scripts/"

VERSION_ID="${DEPLOYMENT_VERSION:-$(date +%Y%m%d.%H%M%S)}"
BUILD_TIME="$(date +%Y-%m-%dT%H:%M:%S%z)"
COMMIT="$(git -C "${ROOT_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BRANCH="$(git -C "${ROOT_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
if [ -n "$(git -C "${ROOT_DIR}" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  DIRTY=1
else
  DIRTY=0
fi
SEMANTIC_VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "${ROOT_DIR}/src/qwenpaw/__version__.py" | head -1)"
KNOWLEDGE_VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "${ROOT_DIR}/knowledge-service/pyproject.toml" | head -1)"
cat > "${APP_DIR}/VERSION" <<EOF
version=${VERSION_ID}
semantic_version=${SEMANTIC_VERSION}
knowledge_version=${KNOWLEDGE_VERSION}
build=${BUILD_TIME}
commit=${COMMIT}
branch=${BRANCH}
dirty=${DIRTY}
package=${PACKAGE_NAME}
arch=$(uname -m)
EOF

find "${APP_DIR}" \
  \( -name "__pycache__" -o -name "*.pyc" -o -name "*.pyo" -o -name ".DS_Store" \) \
  -prune -exec rm -rf {} +
rm -rf "${APP_DIR}/src/copaw.egg-info" "${APP_DIR}/src/qwenpaw.egg-info"

# Code-mount deployments run the app as root. Removing group/other bits keeps
# a UID-isolated ordinary-user shell from reading application source (the app
# itself still reads it as root). This is what replaces Landlock's hiding of
# /workspace/app on kernels without Landlock.
chmod -R go-rwx "${APP_DIR}"

tar -C "${DIST_DIR}" -czf "${DIST_DIR}/${PACKAGE_NAME}" app
shasum -a 256 "${DIST_DIR}/${PACKAGE_NAME}" > "${DIST_DIR}/${PACKAGE_NAME}.sha256"

echo "Code update package: ${DIST_DIR}/${PACKAGE_NAME}"
echo "Checksum: ${DIST_DIR}/${PACKAGE_NAME}.sha256"
