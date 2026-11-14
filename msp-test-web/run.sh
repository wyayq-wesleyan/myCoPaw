#!/bin/sh
set -e

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
IMAGE="${BASE_IMAGE:-huizhi-runtime:2.2.1-amd64}"
PORT="${MSP_TEST_PORT:-18080}"
BASE_URL="${MIDDLEWARE_BASE_URL:-http://127.0.0.1:9089}"
SYSTEM_CODE="${MSP_TEST_SYSTEM_CODE:-middleware}"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found" >&2
    exit 1
fi

echo "联调测试台 / Integration Console"
echo "  image:      $IMAGE"
echo "  middleware: $BASE_URL"
echo "  listen:     http://0.0.0.0:$PORT"
echo "  open:       http://<本机内网IP>:$PORT"
echo

exec docker run --rm \
    --name integration-console \
    --network host \
    -e MIDDLEWARE_BASE_URL="$BASE_URL" \
    -e MSP_TEST_SYSTEM_CODE="$SYSTEM_CODE" \
    -e MSP_TEST_TCMS_SYSTEM_CODE="${MSP_TEST_TCMS_SYSTEM_CODE:-}" \
    -e MSP_TEST_ELC_SYSTEM_CODE="${MSP_TEST_ELC_SYSTEM_CODE:-}" \
    -e MSP_TEST_PORT="$PORT" \
    -e MSP_TEST_SSO_LOGOUT_URL="${MSP_TEST_SSO_LOGOUT_URL:-}" \
    -e MSP_TEST_VERIFY_TLS="${MSP_TEST_VERIFY_TLS:-false}" \
    -e MSP_TEST_SECRET_KEY="${MSP_TEST_SECRET_KEY:-integration-console-internal-only}" \
    -e PYTHONUNBUFFERED=1 \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e TZ="${TZ:-Asia/Shanghai}" \
    -v "$SCRIPT_DIR:/opt/msp-test-web:ro" \
    --entrypoint python \
    "$IMAGE" /opt/msp-test-web/app.py
