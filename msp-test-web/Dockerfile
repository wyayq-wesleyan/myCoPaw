ARG BASE_IMAGE=huizhi-runtime:2.2.1-amd64
FROM ${BASE_IMAGE}

WORKDIR /opt/msp-test-web
COPY app.py /opt/msp-test-web/app.py
COPY console /opt/msp-test-web/console
COPY templates /opt/msp-test-web/templates
COPY static /opt/msp-test-web/static

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MSP_TEST_HOST=0.0.0.0 \
    MSP_TEST_PORT=18080

EXPOSE 18080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18080/healthz', timeout=3)" || exit 1

ENTRYPOINT ["python", "/opt/msp-test-web/app.py"]
