ARG BASE_IMAGE=py311-base:1.0.0
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

# -----------------------------------------------------------------------------
# Stage 1: build console frontend (dist not committed in repo).
# -----------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS console-builder
WORKDIR /app
COPY console /app/console
ARG NPM_REGISTRY
RUN npm config set registry ${NPM_REGISTRY} \
 && cd /app/console \
 && npm ci --include=dev \
 && npm run build

# -----------------------------------------------------------------------------
# Stage 2: runtime image with Python, Chromium, and app.
# -----------------------------------------------------------------------------
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST

# ENV variables
ENV NODE_ENV=production
ENV WORKSPACE_DIR=/app
ENV COPAW_WORKING_DIR=/app/working
ENV COPAW_SECRET_DIR=/app/working.secret

# Channel filtering: use COPAW_DISABLED_CHANNELS (exclusion, recommended)
# or COPAW_ENABLED_CHANNELS (whitelist). Override at runtime with -e.
ARG COPAW_DISABLED_CHANNELS="imessage"
ENV COPAW_DISABLED_CHANNELS=${COPAW_DISABLED_CHANNELS}
ARG COPAW_ENABLED_CHANNELS=""
ENV COPAW_ENABLED_CHANNELS=${COPAW_ENABLED_CHANNELS}

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Indicate running in container (used e.g. for Chromium --no-sandbox).
ENV COPAW_RUNNING_IN_CONTAINER=1

WORKDIR ${WORKSPACE_DIR}

COPY pyproject.toml setup.py README.md ./
COPY src ./src
# Inject console dist from build stage (repo does not commit dist).
COPY --from=console-builder /app/console/dist/ ./src/copaw/console/
RUN pip install --no-cache-dir --index-url ${PIP_INDEX_URL} --trusted-host ${PIP_TRUSTED_HOST} --upgrade pip setuptools wheel \
 && pip install --no-cache-dir --index-url ${PIP_INDEX_URL} --trusted-host ${PIP_TRUSTED_HOST} .[ollama] \
 && pip install --no-cache-dir --index-url ${PIP_INDEX_URL} --trusted-host ${PIP_TRUSTED_HOST} reportlab \
 && python - <<'PY'
from pathlib import Path

stream_path = Path(
    "/usr/local/lib/python3.11/site-packages/"
    "agentscope_runtime/adapters/agentscope/stream.py"
)
text = stream_path.read_text(encoding="utf-8")
old = "                                if completed_content.text:\n                                    yield completed_content.completed()\n"
new = "                                completed_text = getattr(completed_content, \"text\", None)\n                                if completed_text:\n                                    yield completed_content.completed()\n"
if old not in text:
    raise SystemExit(f"Expected patch target not found in {stream_path}")
stream_path.write_text(text.replace(old, new), encoding="utf-8")
print(f"Patched {stream_path}")
PY

RUN python - <<'PY'
from importlib.util import find_spec
mods = [
    "playwright",
    "markitdown",
    "openpyxl",
    "pypdf",
    "pdfplumber",
    "pdf2image",
    "pytesseract",
    "pypdfium2",
    "docx",
    "pptx",
]
missing = [m for m in mods if find_spec(m) is None]
if missing:
    raise SystemExit(f"Missing python modules: {missing}")
print("offline python dependency smoke test OK")
PY

RUN pandoc --version | head -n 1 \
 && soffice --version \
 && pdftotext -v | head -n 1 \
 && pdftoppm -v | head -n 1 \
 && tesseract --version | head -n 1 \
 && node --version \
 && npm --version

# Init working dir with default config.json and HEARTBEAT.md (build time).
RUN copaw init --defaults --accept-security

# CoPaw app port (default 8088). Override at runtime with -e COPAW_PORT=3000.
ENV COPAW_PORT=8088

COPY deploy/config/supervisord.conf.template /etc/supervisor/conf.d/supervisord.conf.template
COPY --chmod=755 deploy/entrypoint.sh /entrypoint.sh

EXPOSE 8088

CMD ["/entrypoint.sh"]
