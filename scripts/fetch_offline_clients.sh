#!/usr/bin/env bash
# Download redistributable offline client archives into arch-specific folders.
# Oracle packages are intentionally excluded because they usually require
# manual download and license acceptance.
#
# Supported versions:
#   HADOOP_VERSION  - Hadoop version (default: 3.3.6, also supports 3.0.1)
#   HIVE2_VERSION   - Hive 2.x version (default: 2.3.9, compatible with Hadoop 2.x-3.x)
#   HIVE3_VERSION   - Hive 3.x version (default: 3.1.3, requires Hadoop >= 3.1.0)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ARCH="${1:-$(uname -m)}"
case "$ARCH" in
  amd64|x86_64) TARGET_ARCH="amd64" ;;
  arm64|aarch64) TARGET_ARCH="arm64" ;;
  *)
    echo "[fetch_offline_clients] Unsupported arch: $ARCH" >&2
    exit 1
    ;;
esac

# Default versions
HADOOP_VERSION="${HADOOP_VERSION:-3.3.6}"
HIVE2_VERSION="${HIVE2_VERSION:-2.3.9}"
HIVE3_VERSION="${HIVE3_VERSION:-3.1.3}"

OUT_ROOT="$REPO_ROOT/deploy/offline-assets/$TARGET_ARCH"
mkdir -p "$OUT_ROOT/hadoop" "$OUT_ROOT/hive2" "$OUT_ROOT/hive3" "$OUT_ROOT/oracle"

if [[ "$TARGET_ARCH" == "arm64" ]]; then
  HADOOP_FILE="hadoop-${HADOOP_VERSION}-aarch64.tar.gz"
else
  HADOOP_FILE="hadoop-${HADOOP_VERSION}.tar.gz"
fi
HIVE2_FILE="apache-hive-${HIVE2_VERSION}-bin.tar.gz"
HIVE3_FILE="apache-hive-${HIVE3_VERSION}-bin.tar.gz"

HADOOP_URLS=(
  "https://mirrors.tuna.tsinghua.edu.cn/apache/hadoop/common/hadoop-${HADOOP_VERSION}/${HADOOP_FILE}"
  "https://mirrors.aliyun.com/apache/hadoop/common/hadoop-${HADOOP_VERSION}/${HADOOP_FILE}"
  "https://mirrors.huaweicloud.com/apache/hadoop/common/hadoop-${HADOOP_VERSION}/${HADOOP_FILE}"
  "https://mirrors.tencent.com/apache/hadoop/common/hadoop-${HADOOP_VERSION}/${HADOOP_FILE}"
  "https://dlcdn.apache.org/hadoop/common/hadoop-${HADOOP_VERSION}/${HADOOP_FILE}"
  "https://archive.apache.org/dist/hadoop/common/hadoop-${HADOOP_VERSION}/${HADOOP_FILE}"
)
HIVE2_URLS=(
  "https://mirrors.huaweicloud.com/apache/hive/hive-${HIVE2_VERSION}/${HIVE2_FILE}"
  "https://mirrors.tuna.tsinghua.edu.cn/apache/hive/hive-${HIVE2_VERSION}/${HIVE2_FILE}"
  "https://mirrors.aliyun.com/apache/hive/hive-${HIVE2_VERSION}/${HIVE2_FILE}"
  "https://mirrors.tencent.com/apache/hive/hive-${HIVE2_VERSION}/${HIVE2_FILE}"
  "https://dlcdn.apache.org/hive/hive-${HIVE2_VERSION}/${HIVE2_FILE}"
  "https://archive.apache.org/dist/hive/hive-${HIVE2_VERSION}/${HIVE2_FILE}"
)
HIVE3_URLS=(
  "https://mirrors.huaweicloud.com/apache/hive/hive-${HIVE3_VERSION}/${HIVE3_FILE}"
  "https://mirrors.tuna.tsinghua.edu.cn/apache/hive/hive-${HIVE3_VERSION}/${HIVE3_FILE}"
  "https://mirrors.aliyun.com/apache/hive/hive-${HIVE3_VERSION}/${HIVE3_FILE}"
  "https://mirrors.tencent.com/apache/hive/hive-${HIVE3_VERSION}/${HIVE3_FILE}"
  "https://dlcdn.apache.org/hive/hive-${HIVE3_VERSION}/${HIVE3_FILE}"
  "https://archive.apache.org/dist/hive/hive-${HIVE3_VERSION}/${HIVE3_FILE}"
)

download_if_missing() {
  local path="$1"
  if [[ -f "$path" ]]; then
    if tar -tzf "$path" >/dev/null 2>&1; then
      echo "[fetch_offline_clients] Exists: $path"
      return
    fi
    echo "[fetch_offline_clients] Incomplete archive detected, resuming: $path"
  fi
  shift
  local url
  for url in "$@"; do
    echo "[fetch_offline_clients] Downloading: $url"
    if curl -fLC - --retry 3 --retry-delay 2 -o "$path" "$url" && tar -tzf "$path" >/dev/null 2>&1; then
      return
    fi
    rm -f "$path"
  done
  echo "[fetch_offline_clients] Failed to download to $path" >&2
  return 1
}

echo "[fetch_offline_clients] Fetching Hadoop ${HADOOP_VERSION} for ${TARGET_ARCH}..."
download_if_missing "$OUT_ROOT/hadoop/$HADOOP_FILE" "${HADOOP_URLS[@]}"

echo "[fetch_offline_clients] Fetching Hive 2.x (${HIVE2_VERSION}) for ${TARGET_ARCH}..."
download_if_missing "$OUT_ROOT/hive2/$HIVE2_FILE" "${HIVE2_URLS[@]}"

echo "[fetch_offline_clients] Fetching Hive 3.x (${HIVE3_VERSION}) for ${TARGET_ARCH}..."
download_if_missing "$OUT_ROOT/hive3/$HIVE3_FILE" "${HIVE3_URLS[@]}"

echo "[fetch_offline_clients] Done for $TARGET_ARCH"
echo "[fetch_offline_clients] Place Oracle packages manually in $OUT_ROOT/oracle/"
echo ""
echo "Summary:"
echo "  Hadoop ${HADOOP_VERSION}: $OUT_ROOT/hadoop/$HADOOP_FILE"
echo "  Hive 2.x ${HIVE2_VERSION}: $OUT_ROOT/hive2/$HIVE2_FILE"
echo "  Hive 3.x ${HIVE3_VERSION}: $OUT_ROOT/hive3/$HIVE3_FILE"
