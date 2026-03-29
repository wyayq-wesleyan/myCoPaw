# Changelog

This file tracks the local `mycopaw` fork on top of upstream CoPaw.

Base upstream:

- `v0.2.0.post1`
- commit `21204d6`

Current working branch:

- `codex/upgrade-v0.2.0.post1`

## [Unreleased] - 2026-03-29

### Added

- Added a dedicated console tool card for `send_file_to_user` so generated files are shown with a clear download action in the chat UI instead of only raw tool output text.
- Added a project status summary to `README_zh.md` for session handoff and continued development.

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
