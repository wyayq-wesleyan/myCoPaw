#!/bin/sh
# Runtime entrypoint for code-mounted offline deployments.
set -e

export QWENPAW_PORT="${QWENPAW_PORT:-8088}"
export QWENPAW_AUTH_ENABLED="${QWENPAW_AUTH_ENABLED:-true}"
export QWENPAW_AUTH_USERNAME="${QWENPAW_AUTH_USERNAME:-admin}"
export QWENPAW_AUTH_PASSWORD="${QWENPAW_AUTH_PASSWORD:-Admin@123456}"
export QWENPAW_FORCE_AUTH_ON_LOCALHOST="${QWENPAW_FORCE_AUTH_ON_LOCALHOST:-true}"
export QWENPAW_ENABLED_CHANNELS="${QWENPAW_ENABLED_CHANNELS:-console}"

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

exec /entrypoint.sh
