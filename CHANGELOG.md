# Changelog

This file tracks the local `mycopaw` fork on top of upstream CoPaw / QwenPaw.

Base upstream:

- historical base: `v0.2.0.post1`
- current official sync source: `qwenpaw 1.1.12.post1` from official PyPI package

## [Unreleased] - 2026-06-19

### Added

- Synced the local fork to the latest official Python core available from the official package source: `qwenpaw 1.1.12.post1`.
- Added a compatibility layout so the repo now carries:
  - `src/qwenpaw` as the latest official primary codebase
  - `src/copaw` as a compatibility symlink for legacy imports and tooling
- Expanded the reusable offline base image with a broader internal-use dependency set:
  - mainstream database drivers
  - data engineering and file-format libraries
  - container / Kubernetes / server-management libraries
  - CLI tools for network, SSH, Redis, PostgreSQL, MySQL, and diagnostics
- Updated the app image to default to the official packaged console assets instead of rebuilding frontend assets during every app image build.
- Added a lightweight multi-user isolation layer for authenticated deployments:
  - each registered user gets a private default agent and workspace root
  - agent visibility, agent CRUD, active-agent resolution, and file preview are now owner-scoped
  - generated files sent by `send_file_to_user` stay inside the active user's workspace media area
- Added admin-only protection for global operational endpoints such as environment variables, provider management, local-model management, and backup management.

### Changed

- The local fork is no longer anchored only on `v0.2.0.post1`; it now tracks the newer official `QwenPaw` code line while preserving `copaw` command compatibility.
- Oracle client handling in the base image is now broader: `amd64` can accept Oracle 11g or 19c/21c/23c Instant Client packages.
- The offline asset flow now documents the latest official-code sync strategy and packaged console usage.
- Authentication storage now supports multiple users instead of a single legacy account record, with username migration preserving user-owned workspaces.

### Fixed

- Re-applied generated-file lifecycle management on top of the latest official `send_file_to_user` implementation, so downloadable artifacts can still be copied into a managed workspace area and cleaned up over time.
- Fixed OAuth callback handling under authenticated deployments by allowing callback endpoints to complete without a bearer token while still binding the result to the stored OAuth session state.
- Removed a multi-user leakage fallback where MCP OAuth token persistence could previously fall back to the global active agent if the session target agent was missing.

## [2026-03-30] - Offline Base Expansion

### Added

- Added multi-version Hive support: both Hive 2.x (2.3.9) and Hive 3.x (3.1.3) can be installed simultaneously.
  - `deploy/offline-assets/<arch>/hive2/` for Hive 2.x packages
  - `deploy/offline-assets/<arch>/hive3/` for Hive 3.x packages
  - `HIVE2_HOME=/opt/hive2` and `HIVE3_HOME=/opt/hive3` environment variables
  - Hive 3.x is installed to `/opt/hive` by default, Hive 2.x to `/opt/hive2`
  - Runtime can switch versions via `HIVE_HOME` environment variable
- Added Hadoop 3.0.1 support (in addition to 3.3.6) for production environment compatibility.
- Updated `scripts/fetch_offline_clients.sh` to download Hadoop 3.3.6, Hive 2.3.9, and Hive 3.1.3 by default.
- Updated `deploy/README_zh.md` with detailed multi-version Hive documentation.
- Updated Oracle Instant Client requirement from 11g to 19c/21c/23c to match production environment.

### Changed

- The current development focus is now explicitly local `arm64` validation first, with production `x86_64` adaptation deferred until the local flow is stable.
- The reusable base image path has been standardized around `py311-base:1.0.0`.

### Fixed

- Fixed `send_file_to_user` relative-path resolution so files generated in the active workspace can be sent without forcing the agent to switch to absolute paths.
- Fixed the local console file-delivery UX by exposing downloadable links through `/api/console/files/{agent_id}/{filename}` and rendering them in the web UI.
- Added generated-file lifecycle controls for console-delivered artifacts via:
  - `COPAW_GENERATED_FILE_TTL_HOURS`
  - `COPAW_GENERATED_FILE_MAX_FILES`
  - `COPAW_GENERATED_FILE_MAX_TOTAL_MB`

### Verified

- Verified local model connectivity from container to host model service through `host.docker.internal`.
- Verified local `arm64` test container can generate a file, return a download link, and download the generated artifact successfully through the console API.

## [2026-03-26] - File Delivery And Local ARM Debugging

### Fixed

- Identified that `send_file_to_user("relative-path")` failed after `write_file(...)` because the tool checked the process current directory instead of the active workspace.
- Patched file resolution to prefer the current workspace and then fall back to the configured working directory.

### Changed

- Confirmed the console backend returns valid download URLs for generated files and that the failure mode was primarily in tool resolution plus frontend discoverability.

## [2026-03-23] - Offline Deployment Merge

### Added

- Merged earlier offline/localization work into upstream `v0.2.0.post1`.
- Added reusable offline-friendly image build scripts:
  - `scripts/docker_build_base.sh`
  - `scripts/docker_build.sh`
  - `scripts/docker_build_matrix.sh`
  - `scripts/fetch_offline_clients.sh`
- Added `deploy/Dockerfile.base` to build a shared Python 3.11 base image with Chinese mainland mirrors, browser support, document processing, database drivers, and big-data client hooks.

### Changed

- Standardized the app image to build on a reusable base image instead of repeatedly downloading dependencies.
- Added offline asset loading hooks for:
  - Hadoop client archives
  - Hive client archives
  - Oracle Instant Client packages

### Packaging Notes

- `amd64` base image requires an Oracle 11g Instant Client package under `deploy/offline-assets/amd64/oracle/`.
- `arm64` local testing can proceed without Oracle packaging for now.

## Reference Commits

- `8debb5d` `Merge offline deployment and file delivery into v0.2.0`
- `5e502bc` `Expand base image packages for ops and database automation`
