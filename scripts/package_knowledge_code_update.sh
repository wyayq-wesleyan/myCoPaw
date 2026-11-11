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
# CPU/memory profile as well. Both architectures travel — production is x86_64,
# developer machines are often arm64, and only the default image tag differs.
cp "${ROOT_DIR}/knowledge-service/docker-compose.code-mount.amd64.yml" \
   "${ROOT_DIR}/knowledge-service/docker-compose.code-mount.arm64.yml" \
   "${ROOT_DIR}/knowledge-service/docker-compose.postgres.yml" \
   "${ROOT_DIR}/knowledge-service/.env.example" \
   "${STAGE_DIR}/knowledge-service/"
# docker-compose.postgres.yml bind-mounts ./initdb, so the directory has to
# travel with it. Without it a fresh data directory skips the graph-database
# init script and multi-tenant graph storage silently has no database to use
# (Docker creates the empty host path rather than failing, so the omission is
# invisible at start-up).
if [[ -d "${ROOT_DIR}/knowledge-service/initdb" ]]; then
  cp -R "${ROOT_DIR}/knowledge-service/initdb" "${STAGE_DIR}/knowledge-service/"
fi
VERSION_ID="${DEPLOYMENT_VERSION:-$(date +%Y%m%d.%H%M%S)}"
BUILD_TIME="$(date +%Y-%m-%dT%H:%M:%S%z)"
COMMIT="$(git -C "${ROOT_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BRANCH="$(git -C "${ROOT_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
if [ -n "$(git -C "${ROOT_DIR}" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  DIRTY=1
else
  DIRTY=0
fi
KNOWLEDGE_VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "${ROOT_DIR}/knowledge-service/pyproject.toml" | head -1)"
cat > "${STAGE_DIR}/knowledge-service/VERSION" <<EOF
version=${VERSION_ID}
knowledge_version=${KNOWLEDGE_VERSION}
build=${BUILD_TIME}
commit=${COMMIT}
branch=${BRANCH}
dirty=${DIRTY}
package=${PACKAGE_NAME}
arch=$(uname -m)
EOF

find "${STAGE_DIR}" \( -name __pycache__ -o -name '*.pyc' -o -name '*.pyo' -o -name .DS_Store \) -prune -exec rm -rf {} +

tar -C "${STAGE_DIR}" -czf "${DIST_DIR}/${PACKAGE_NAME}" knowledge-service
shasum -a 256 "${DIST_DIR}/${PACKAGE_NAME}" > "${DIST_DIR}/${PACKAGE_NAME}.sha256"
printf 'Knowledge code update package: %s\nChecksum: %s\n' \
  "${DIST_DIR}/${PACKAGE_NAME}" "${DIST_DIR}/${PACKAGE_NAME}.sha256"
