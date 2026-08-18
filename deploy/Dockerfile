ARG BASE_IMAGE=py311-base:2.0.1-arm64
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST
ARG QWENPAW_DISABLED_CHANNELS=""
ARG QWENPAW_ENABLED_CHANNELS="console"

ENV NODE_ENV=production \
    WORKSPACE_DIR=/app \
    QWENPAW_WORKING_DIR=/app/working \
    QWENPAW_SECRET_DIR=/app/working.secret \
    QWENPAW_BACKUP_DIR=/app/working.backups \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    QWENPAW_RUNNING_IN_CONTAINER=1 \
    QWENPAW_DISABLED_CHANNELS=${QWENPAW_DISABLED_CHANNELS} \
    QWENPAW_ENABLED_CHANNELS=${QWENPAW_ENABLED_CHANNELS} \
    QWENPAW_PORT=8088

WORKDIR ${WORKSPACE_DIR}

COPY pyproject.toml setup.py README.md ./
COPY src ./src
COPY plugins/bundle/omp_workflows /opt/huizhi/offline-plugins/omp-workflows
COPY website/public/docs/ ./src/qwenpaw/docs/

RUN python -m pip install --no-cache-dir \
      --index-url ${PIP_INDEX_URL} \
      --trusted-host ${PIP_TRUSTED_HOST} \
      --upgrade pip setuptools wheel \
 && python -m pip install --no-cache-dir \
      --index-url ${PIP_INDEX_URL} \
      --trusted-host ${PIP_TRUSTED_HOST} \
      .[local] \
 && python - <<'PY'
from importlib.util import find_spec

required = [
    "agentscope", "reme", "playwright", "fitz", "openpyxl", "pypdf",
    "pdfplumber", "docx", "fastapi", "oracledb", "pyhive", "impala",
]
missing = [name for name in required if find_spec(name) is None]
if missing:
    raise SystemExit(f"Missing runtime modules: {missing}")
PY

RUN qwenpaw init --defaults --accept-security

COPY deploy/config/supervisord.conf.template /etc/supervisor/conf.d/supervisord.conf.template
COPY --chmod=755 deploy/entrypoint.sh /entrypoint.sh

EXPOSE 8088

CMD ["/entrypoint.sh"]
