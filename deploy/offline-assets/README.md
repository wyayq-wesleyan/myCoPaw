# Offline Build Assets

Do not commit binary client packages to the source repository.

Before building a target-architecture base image, provide the verified local
assets under `deploy/offline-assets/<arch>/`:

- `amd64/oracle/` for the production Oracle Instant Client packages;
- `amd64` or `arm64` Hadoop and Hive archives where required;
- any additional browser or plugin binaries that cannot be fetched during an
  air-gapped build.

The shared base Dockerfile consumes these assets when present. The ARM64
development image may omit Oracle.
