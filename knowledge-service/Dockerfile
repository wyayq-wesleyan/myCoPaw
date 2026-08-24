ARG BASE_IMAGE=py311-base:2.0.1-arm64
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST
ARG INSTALL_KNOWLEDGE_EXTRAS=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KNOWLEDGE_DATA_DIR=/data \
    PARSER_BACKEND=auto

WORKDIR /opt/huizhi-knowledge
COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts

RUN python -m pip install --no-cache-dir --no-build-isolation \
      --index-url ${PIP_INDEX_URL} \
      --trusted-host ${PIP_TRUSTED_HOST} \
      . \
 && if [ "${INSTALL_KNOWLEDGE_EXTRAS}" = "1" ]; then \
      python -m pip install --no-cache-dir \
        --index-url ${PIP_INDEX_URL} \
        --trusted-host ${PIP_TRUSTED_HOST} \
        'torch' 'torchvision' \
        'lightrag-hku==1.5.6' 'mineru[core]==3.4.5'; \
    fi

EXPOSE 18100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18100"]
