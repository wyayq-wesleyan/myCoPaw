#!/bin/sh
# Runtime entrypoint for code-mounted offline deployments.
set -e

export QWENPAW_PORT="${QWENPAW_PORT:-8088}"
export QWENPAW_AUTH_ENABLED="${QWENPAW_AUTH_ENABLED:-true}"
export QWENPAW_AUTH_USERNAME="${QWENPAW_AUTH_USERNAME:-admin}"
export QWENPAW_AUTH_PASSWORD="${QWENPAW_AUTH_PASSWORD:-Admin@123456}"
export QWENPAW_FORCE_AUTH_ON_LOCALHOST="${QWENPAW_FORCE_AUTH_ON_LOCALHOST:-true}"
export QWENPAW_ENABLED_CHANNELS="${QWENPAW_ENABLED_CHANNELS:-console}"
export QWENPAW_LOG_LEVEL="${QWENPAW_LOG_LEVEL:-info}"
export QWENPAW_LOG_DIR="${QWENPAW_LOG_DIR:-${QWENPAW_WORKING_DIR:-/app/working}/logs}"
mkdir -p "${QWENPAW_LOG_DIR}"

# Patch older base-image Supervisor templates at runtime so app logs are
# forwarded to Docker stdout/stderr without rebuilding the base image.
python - <<'PY'
from pathlib import Path
import re

path = Path('/etc/supervisor/conf.d/supervisord.conf.template')
if path.exists() and path.is_file():
    value = path.read_text(encoding='utf-8')
    value = re.sub(r'^logfile=.*$', 'logfile=/dev/null', value, flags=re.MULTILINE)
    value = re.sub(r'^logfile_(?:maxbytes|backups)=.*$\n?', '', value, flags=re.MULTILINE)
    value = re.sub(r'^stdout_logfile_maxbytes=.*$\n?', '', value, flags=re.MULTILINE)
    value = re.sub(r'^stdout_logfile_backups=.*$\n?', '', value, flags=re.MULTILINE)
    value = re.sub(r'^stderr_logfile_maxbytes=.*$\n?', '', value, flags=re.MULTILINE)
    value = re.sub(r'^stderr_logfile_backups=.*$\n?', '', value, flags=re.MULTILINE)
    # Older base images may still ship a /dev/fd-only template. Redirect all
    # Supervisor output into the mounted log directory so it survives browser
    # crashes and can be inspected directly on the host.
    value = re.sub(
        r'^stdout_logfile=.*$',
        'stdout_logfile=${QWENPAW_LOG_DIR}/supervisor-%(program_name)s.stdout.log\n'
        'stdout_logfile_maxbytes=20MB\nstdout_logfile_backups=5',
        value,
        flags=re.MULTILINE,
    )
    value = re.sub(
        r'^stderr_logfile=.*$',
        'stderr_logfile=${QWENPAW_LOG_DIR}/supervisor-%(program_name)s.stderr.log\n'
        'stderr_logfile_maxbytes=20MB\nstderr_logfile_backups=5',
        value,
        flags=re.MULTILINE,
    )
    value = re.sub(r'^logfile=.*$', 'logfile=${QWENPAW_LOG_DIR}/supervisord.log\nlogfile_maxbytes=20MB\nlogfile_backups=5', value, flags=re.MULTILINE)
    value = re.sub(r'^command=qwenpaw app .*$', 'command=qwenpaw app --host 0.0.0.0 --port ${COPAW_PORT} --log-level ${QWENPAW_LOG_LEVEL}', value, flags=re.MULTILINE)
    path.write_text(value, encoding='utf-8')
PY

export QWENPAW_CODE_DIR="${QWENPAW_CODE_DIR:-/workspace/app}"
export PYTHONPATH="${QWENPAW_CODE_DIR}/src${PYTHONPATH:+:$PYTHONPATH}"
export QWENPAW_CONSOLE_STATIC_DIR="${QWENPAW_CONSOLE_STATIC_DIR:-${QWENPAW_CODE_DIR}/console/dist}"

# The curated workflow plugin has no network or package-install dependency.
# Seed it once into the persistent plugin directory so source mounts and full
# images expose the same offline capability without overwriting user changes.
offline_plugin_source="${QWENPAW_CODE_DIR}/plugins/bundle/omp_workflows"
offline_plugin_target="${QWENPAW_WORKING_DIR:-/app/working}/plugins/omp-workflows"
if [ -d "${offline_plugin_source}" ] && [ ! -d "${offline_plugin_target}" ]; then
  mkdir -p "$(dirname "${offline_plugin_target}")"
  cp -a "${offline_plugin_source}" "${offline_plugin_target}"
fi

# Files extracted from a code update package do not necessarily retain an
# executable bit on every server/filesystem. Invoke the mounted script via
# the shell so code-mount startup never depends on that host-side metadata.
exec /bin/sh /entrypoint.sh
