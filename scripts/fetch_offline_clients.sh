#!/usr/bin/env bash
# Download redistributable offline client archives into arch-specific folders.
# Oracle packages are intentionally excluded because they usually require
# manual download and license acceptance.
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

HADOOP_VERSION="${HADOOP_VERSION:-3.3.6}"
HIVE_VERSION="${HIVE_VERSION:-3.1.3}"
OUT_ROOT="$REPO_ROOT/deploy/offline-assets/$TARGET_ARCH"
mkdir -p "$OUT_ROOT/hadoop" "$OUT_ROOT/hive" "$OUT_ROOT/oracle"

if [[ "$TARGET_ARCH" == "arm64" ]]; then
  HADOOP_FILE="hadoop-${HADOOP_VERSION}-aarch64.tar.gz"
else
  HADOOP_FILE="hadoop-${HADOOP_VERSION}.tar.gz"
fi
HIVE_FILE="apache-hive-${HIVE_VERSION}-bin.tar.gz"

HADOOP_URLS=(
  "https://mirrors.tuna.tsinghua.edu.cn/apache/hadoop/common/hadoop-${HADOOP_VERSION}/${HADOOP_FILE}"
  "https://mirrors.aliyun.com/apache/hadoop/common/hadoop-${HADOOP_VERSION}/${HADOOP_FILE}"
  "https://archive.apache.org/dist/hadoop/common/hadoop-${HADOOP_VERSION}/${HADOOP_FILE}"
)
HIVE_URLS=(
  "https://mirrors.huaweicloud.com/apache/hive/hive-${HIVE_VERSION}/${HIVE_FILE}"
  "https://mirrors.tuna.tsinghua.edu.cn/apache/hive/hive-${HIVE_VERSION}/${HIVE_FILE}"
  "https://mirrors.aliyun.com/apache/hive/hive-${HIVE_VERSION}/${HIVE_FILE}"
  "https://archive.apache.org/dist/hive/hive-${HIVE_VERSION}/${HIVE_FILE}"
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

download_if_missing "$OUT_ROOT/hadoop/$HADOOP_FILE" "${HADOOP_URLS[@]}"
download_if_missing "$OUT_ROOT/hive/$HIVE_FILE" "${HIVE_URLS[@]}"

echo "[fetch_offline_clients] Done for $TARGET_ARCH"
echo "[fetch_offline_clients] Place Oracle packages manually in $OUT_ROOT/oracle/"
