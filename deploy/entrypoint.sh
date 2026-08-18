#!/bin/sh
# Substitute COPAW_PORT in supervisord template and start supervisord.
# Default port 8088; override at runtime with -e COPAW_PORT=3000.
set -e
export COPAW_PORT="${COPAW_PORT:-8088}"
export QWENPAW_AUTH_ENABLED="${QWENPAW_AUTH_ENABLED:-true}"
export QWENPAW_AUTH_USERNAME="${QWENPAW_AUTH_USERNAME:-admin}"
export QWENPAW_AUTH_PASSWORD="${QWENPAW_AUTH_PASSWORD:-Admin@123456}"
export QWENPAW_FORCE_AUTH_ON_LOCALHOST="${QWENPAW_FORCE_AUTH_ON_LOCALHOST:-true}"
export QWENPAW_ENABLED_CHANNELS="${QWENPAW_ENABLED_CHANNELS:-console}"

offline_plugin_source="/opt/huizhi/offline-plugins/omp-workflows"
offline_plugin_target="${QWENPAW_WORKING_DIR:-/app/working}/plugins/omp-workflows"
if [ -d "${offline_plugin_source}" ] && [ ! -d "${offline_plugin_target}" ]; then
    mkdir -p "$(dirname "${offline_plugin_target}")"
    cp -a "${offline_plugin_source}" "${offline_plugin_target}"
fi

python - <<'PY'
import json
import os
from pathlib import Path

working_dir = Path(
    os.environ.get("QWENPAW_WORKING_DIR")
    or os.environ.get("COPAW_WORKING_DIR")
    or "/app/working",
)
config_path = working_dir / "config.json"
config_path.parent.mkdir(parents=True, exist_ok=True)

if config_path.exists():
    try:
        config = json.loads(config_path.read_text("utf-8"))
    except Exception:
        config = {}
else:
    config = {}

security = config.setdefault("security", {})
hosts = security.get("allow_no_auth_hosts")
if not isinstance(hosts, list):
    hosts = ["127.0.0.1", "::1"]

if os.environ.get("QWENPAW_FORCE_AUTH_ON_LOCALHOST", "").strip().lower() in {
    "true",
    "1",
    "yes",
}:
    hosts = [
        host for host in hosts
        if host not in {"127.0.0.1", "::1", "localhost"}
    ]

security["allow_no_auth_hosts"] = hosts
config_path.write_text(
    json.dumps(config, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
PY

envsubst '${COPAW_PORT}' \
  < /etc/supervisor/conf.d/supervisord.conf.template \
  > /etc/supervisor/conf.d/supervisord.conf
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
