ARG BASE_IMAGE=py311-base:2.0.1-arm64
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

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

RUN python -m pip install --no-cache-dir \
      --index-url ${PIP_INDEX_URL} \
      --trusted-host ${PIP_TRUSTED_HOST} \
      . \
 && if [ "${INSTALL_KNOWLEDGE_EXTRAS}" = "1" ]; then \
      python -m pip install --no-cache-dir \
        --index-url ${PIP_INDEX_URL} \
        --trusted-host ${PIP_TRUSTED_HOST} \
        'lightrag-hku==1.5.6' 'mineru==3.4.5'; \
    fi

EXPOSE 18100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18100"]
