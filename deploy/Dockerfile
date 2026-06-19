ARG BASE_IMAGE=py311-base:1.0.0
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST

ENV NODE_ENV=production
ENV WORKSPACE_DIR=/app
ENV QWENPAW_WORKING_DIR=/app/working
ENV QWENPAW_SECRET_DIR=/app/working.secret
ENV COPAW_WORKING_DIR=/app/working
ENV COPAW_SECRET_DIR=/app/working.secret

ARG COPAW_DISABLED_CHANNELS="imessage"
ENV COPAW_DISABLED_CHANNELS=${COPAW_DISABLED_CHANNELS}
ENV QWENPAW_DISABLED_CHANNELS=${COPAW_DISABLED_CHANNELS}
ARG COPAW_ENABLED_CHANNELS=""
ENV COPAW_ENABLED_CHANNELS=${COPAW_ENABLED_CHANNELS}
ENV QWENPAW_ENABLED_CHANNELS=${COPAW_ENABLED_CHANNELS}

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV QWENPAW_RUNNING_IN_CONTAINER=1
ENV COPAW_RUNNING_IN_CONTAINER=1

WORKDIR ${WORKSPACE_DIR}

COPY pyproject.toml setup.py README.md ./
COPY src ./src

RUN pip install --no-cache-dir --index-url ${PIP_INDEX_URL} --trusted-host ${PIP_TRUSTED_HOST} --upgrade pip setuptools wheel \
 && pip install --no-cache-dir --index-url ${PIP_INDEX_URL} --trusted-host ${PIP_TRUSTED_HOST} .[ollama] \
 && pip install --no-cache-dir --index-url ${PIP_INDEX_URL} --trusted-host ${PIP_TRUSTED_HOST} reportlab \
 && python - <<'PY'
from pathlib import Path

stream_path = Path(
    "/usr/local/lib/python3.11/site-packages/"
    "agentscope_runtime/adapters/agentscope/stream.py"
)
old = (
    "                                if completed_content.text:\\n"
    "                                    yield completed_content.completed()\\n"
)
new = (
    "                                completed_text = getattr(completed_content, \\\"text\\\", None)\\n"
    "                                if completed_text:\\n"
    "                                    yield completed_content.completed()\\n"
)
if stream_path.exists():
    text = stream_path.read_text(encoding="utf-8")
    if old in text:
        stream_path.write_text(text.replace(old, new), encoding="utf-8")
        print(f\"Patched {stream_path}\")
    else:
        print(f\"Patch target already absent in {stream_path}, skipping\")
else:
    print(f\"Patch target missing: {stream_path}, skipping\")
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
    "fastapi",
    "oracledb",
    "pyhive",
    "impyla",
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

RUN copaw init --defaults --accept-security

ENV COPAW_PORT=8088
ENV QWENPAW_PORT=8088

COPY deploy/config/supervisord.conf.template /etc/supervisor/conf.d/supervisord.conf.template
COPY --chmod=755 deploy/entrypoint.sh /entrypoint.sh

EXPOSE 8088

CMD ["/entrypoint.sh"]
