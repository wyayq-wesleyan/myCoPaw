#!/usr/bin/env bash
# Export the all-in-one runtime image for transfer to an air-gapped server.
#
# One image serves every service (assistant, knowledge-api, knowledge-worker,
# lightrag), so a single archive carries all of them — the shared dependency
# layers are stored once instead of being duplicated across per-service
# tarballs.
#
# Compression choice matters: gzip is slow and barely shrinks an image this
# large. zstd (when available) is much faster and smaller; otherwise the
# archive is stored uncompressed, which is also what `docker load` streams
# fastest. Set COMPRESS=zstd|gzip|none to force one.
#
# Usage:
#   bash scripts/docker_export_image.sh [TAG] [OUTPUT_PATH]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist/docker-images"

TAG="${1:-huizhi-runtime:2.2.1-arm64}"
ARCH="${TAG##*-}"

# Default to zstd: these images are tens of GB, and gzip is slow with little
# benefit at this size while storing uncompressed wastes the most disk. zstd
# is typically ~2x smaller than the raw tar and much faster than gzip.
# Set COMPRESS=gzip|none to override, or give OUTPUT_PATH the matching suffix.
COMPRESS="${COMPRESS:-auto}"
if [[ "${COMPRESS}" == "auto" ]]; then
  if command -v zstd >/dev/null 2>&1; then COMPRESS="zstd"; else COMPRESS="none"; fi
fi

case "${COMPRESS}" in
  zstd) DEFAULT_EXT=".tar.zst" ;;
  gzip) DEFAULT_EXT=".tar.gz" ;;
  *)    DEFAULT_EXT=".tar" ;;
esac
OUTPUT="${2:-${DIST_DIR}/huizhi-runtime-${ARCH}${DEFAULT_EXT}}"

# A caller-supplied suffix wins, so `... output.tar` really means uncompressed.
case "${OUTPUT}" in
  *.tar.zst) COMPRESS="zstd" ;;
  *.tar.gz|*.tgz) COMPRESS="gzip" ;;
  *.tar) COMPRESS="none" ;;
esac

if ! docker image inspect "${TAG}" >/dev/null 2>&1; then
  echo "[export] Image not found: ${TAG}" >&2
  echo "[export] Build it first, e.g. bash scripts/docker_build_allinone.sh ${TAG}" >&2
  exit 1
fi

mkdir -p "${DIST_DIR}"

echo "[export] Image:  ${TAG}"
echo "[export] Output: ${OUTPUT}"
echo "[export] Compression: ${COMPRESS}"

# Export the single tag directly: it is the only image every service needs, and
# naming it explicitly keeps unrelated tags from being swept into the archive.
case "${COMPRESS}" in
  zstd)
    docker save "${TAG}" | zstd -T0 -3 -o "${OUTPUT}" -f
    ;;
  gzip)
    docker save "${TAG}" | gzip -1 > "${OUTPUT}"
    ;;
  none)
    docker save -o "${OUTPUT}" "${TAG}"
    ;;
  *)
    echo "[export] Unknown COMPRESS value: ${COMPRESS}" >&2
    exit 1
    ;;
esac

shasum -a 256 "${OUTPUT}" > "${OUTPUT}.sha256"

SIZE="$(du -h "${OUTPUT}" | cut -f1)"
echo
echo "[export] Done: ${OUTPUT} (${SIZE})"
echo "[export] Checksum: ${OUTPUT}.sha256"
echo
echo "[export] On the server:"
case "${COMPRESS}" in
  zstd) echo "  zstd -dc $(basename "${OUTPUT}") | docker load" ;;
  gzip) echo "  gunzip -c $(basename "${OUTPUT}") | docker load" ;;
  *)    echo "  docker load -i $(basename "${OUTPUT}")" ;;
esac
