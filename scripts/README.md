# Scripts

Run from **repo root**.

## Build wheel (with latest console)

```bash
bash scripts/wheel_build.sh
```

- Builds the console frontend (`console/`), copies `console/dist` to `src/copaw/console/dist`, then builds the wheel. Output: `dist/*.whl`.

## Build website

```bash
bash scripts/website_build.sh
```

- Installs dependencies (pnpm or npm) and runs the Vite build. Output: `website/dist/`.

## Build Docker image

### Fetch offline client archives

```bash
bash scripts/fetch_offline_clients.sh [arm64|amd64]
```

- Downloads official Apache Hadoop/Hive archives into
  `deploy/offline-assets/<arch>/`.
- Uses mainland mirrors first (TUNA/Aliyun/HuaweiCloud), then falls back to
  Apache archive.
- Oracle packages still need manual download into
  `deploy/offline-assets/<arch>/oracle/`.

### Build reusable base image

```bash
bash scripts/docker_build_base.sh [IMAGE_TAG] [EXTRA_ARGS...]
```

- Default tag: `py311-base:1.0.0`. Uses `deploy/Dockerfile.base`.
- The base image includes Python 3.11, common data/web/file-processing packages,
  Playwright Chromium, LibreOffice/OCR tooling, Node.js/npm, and optional
  offline client install hooks for Hadoop/Hive/Oracle.
- The base image preloads broad Python dependencies for generated scripts,
  including `oracledb`, `pyhive[hive_pure_sasl]`, `impyla`,
  `psycopg2-binary`, `matplotlib`, `scikit-learn`, and document/database tools.
- It also includes common ops/runtime libraries such as `paramiko`, `fabric`,
  `sshtunnel`, `celery`, `kafka-python`, `elasticsearch`, `boto3`, `minio`,
  `python-dotenv`, and retry/config helpers.
- Legacy `import cx_Oracle` is supported via a compatibility shim that maps to
  `oracledb`.
- `amd64`: Oracle Instant Client basic package is required and should use Oracle
  11g-compatible package naming.
- `arm64`: Oracle package is optional (build skips Oracle installation).
- Oracle packages should be placed under `deploy/offline-assets/<arch>/oracle/`
  before building.
- Set `PLATFORM=linux/arm64` or `PLATFORM=linux/amd64` when building for a
  specific architecture.
- Put offline client archives under `deploy/offline-assets/<arch>/` before
  building the base image.

### Build CoPaw application image

```bash
bash scripts/docker_build.sh [IMAGE_TAG] [EXTRA_ARGS...]
```

- Default tag: `copaw:latest`. Uses `deploy/Dockerfile` and expects a reusable
  base image first.
- Override the base image with `BASE_IMAGE=<tag> bash scripts/docker_build.sh`.
- Example: `bash scripts/docker_build.sh myreg/copaw:v1 --no-cache`.

## Run Test

```bash
# Run all tests
python scripts/run_tests.py

# Run all unit tests
python scripts/run_tests.py -u

# Run unit tests for a specific module
python scripts/run_tests.py -u providers

# Run integration tests
python scripts/run_tests.py -i

# Run all tests and generate a coverage report
python scripts/run_tests.py -a -c

# Run tests in parallel (requires pytest-xdist)
python scripts/run_tests.py -p

# Show help
python scripts/run_tests.py -h
```
