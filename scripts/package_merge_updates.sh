#!/usr/bin/env bash
# One-shot combined code package: 主应用 app/ + 知识服务 knowledge-service/.
#
# 单独打某一个服务的包只在紧急热修时才需要（用 package_code_update.sh 或
# package_knowledge_code_update.sh）。日常更新统一走本脚本，避免两个包分别
# 上线导致主应用与知识服务版本错配。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist/docker-images"
TODAY="$(date +%Y%m%d)"
PACKAGE_NAME="${1:-huizhi-code-update-${TODAY}.tar.gz}"
STAGE_DIR="$(mktemp -d)"
PART_DIR="$(mktemp -d)"
trap 'rm -rf "${STAGE_DIR}" "${PART_DIR}"' EXIT

mkdir -p "${DIST_DIR}" "${STAGE_DIR}/deploy/config"

# Each part keeps its own established packager (frontend rebuild, plugin
# bundling, compose wiring). They write into DIST_DIR, so read the artefacts
# back from there with the staging names passed in.
ASSISTANT_PART="huizhi-combined-staging-assistant.tar.gz"
KNOWLEDGE_PART="huizhi-combined-staging-knowledge.tar.gz"

echo "==> 打包主应用代码（含前端构建）"
bash "${ROOT_DIR}/scripts/package_code_update.sh" "${ASSISTANT_PART}" >/dev/null
mv "${DIST_DIR}/${ASSISTANT_PART}" "${PART_DIR}/assistant.tar.gz"
rm -f "${DIST_DIR}/${ASSISTANT_PART}.sha256"

echo "==> 打包知识服务代码"
bash "${ROOT_DIR}/scripts/package_knowledge_code_update.sh" "${KNOWLEDGE_PART}" >/dev/null
mv "${DIST_DIR}/${KNOWLEDGE_PART}" "${PART_DIR}/knowledge.tar.gz"
rm -f "${DIST_DIR}/${KNOWLEDGE_PART}.sha256"

tar -xzf "${PART_DIR}/assistant.tar.gz" -C "${STAGE_DIR}"
tar -xzf "${PART_DIR}/knowledge.tar.gz" -C "${STAGE_DIR}"

# The deployment layer carries the code-mount entrypoint and the host-mounted
# log template. Compose files contain only resource wiring (no secrets), so a
# single upload also keeps logs landing on the host. Do not extract deploy/
# over an existing production directory without comparing resource limits.
cp "${ROOT_DIR}/deploy/code-mount-entrypoint.sh" "${STAGE_DIR}/deploy/"
# Both architectures travel. Production is x86_64 while a developer machine is
# often arm64, and the files differ only in which image tag the compose defaults
# to — shipping just one silently hands the other platform the wrong default the
# moment the file is copied over.
cp "${ROOT_DIR}/deploy/docker-compose.code-mount.amd64.yml" "${STAGE_DIR}/deploy/"
cp "${ROOT_DIR}/deploy/docker-compose.code-mount.arm64.yml" "${STAGE_DIR}/deploy/"
cp "${ROOT_DIR}/deploy/entrypoint.sh" "${STAGE_DIR}/deploy/"
cp "${ROOT_DIR}/deploy/config/supervisord.conf.template" "${STAGE_DIR}/deploy/config/"
# Environment template. Placeholders only (the same values are already visible in
# the compose files), and the operator needs it to diff against a live .env — a
# deployment older than a variable has no other way to learn it was added.
cp "${ROOT_DIR}/deploy/.env.example" "${STAGE_DIR}/deploy/"
# The upgrade/migration record travels with the package. It is what says which
# changes need an operator action versus which apply themselves, and an operator
# staring at an older server has no other copy: it lives at the repository root,
# which the per-service packagers do not stage.
cp "${ROOT_DIR}/UPGRADE_AND_MIGRATION.md" "${STAGE_DIR}/UPGRADE_AND_MIGRATION.md"

find "${STAGE_DIR}" \
  \( -name __pycache__ -o -name '*.pyc' -o -name '*.pyo' -o -name .DS_Store \) \
  -prune -exec rm -rf {} +

tar -C "${STAGE_DIR}" -czf "${DIST_DIR}/${PACKAGE_NAME}" \
  app knowledge-service deploy UPGRADE_AND_MIGRATION.md
(cd "${DIST_DIR}" && shasum -a 256 "${PACKAGE_NAME}") > "${DIST_DIR}/${PACKAGE_NAME}.sha256"

echo
echo "Combined code update package: ${DIST_DIR}/${PACKAGE_NAME}"
echo "Checksum: ${DIST_DIR}/${PACKAGE_NAME}.sha256"
