# myCoPaw 2.0.1 Migration Plan

This directory is the clean upstream QwenPaw v2.0.1 baseline. The existing
offline fork remains in `mycopaw` until each migration item is verified on
ARM64.

## Findings Before Migration

### Scroll Context

The upstream `scroll` strategy persists conversation turns in a workspace
`history.db`, maintains an eviction index, and exposes `recall_history` (plus
the optional sandboxed `recall_history_python`) to the agent. It addresses
model-context growth and durable history recall.

The upstream console virtualizes the left session list with `react-window`.
The chat message body is still rendered by the chat component and is not
virtualized by Scroll Context. Therefore this feature cannot be assumed to
fix the old browser freeze while scrolling a long chat transcript. That must
be tested separately after migration.

### Offline Plugins

The v2.0.1 plugin loader copies a plugin directory and installs missing
`requirements.txt` entries with `pip` or `uv`. It does not provide an offline
wheelhouse mode by itself.

For an air-gapped deployment, every plugin must be prepared on a connected
machine for the target architecture, including:

1. plugin source and manifest;
2. all Python wheels, including transitive dependencies;
3. non-Python assets or executables used by the plugin;
4. a deterministic offline installer using `--no-index --find-links` or
   dependencies preinstalled in the shared base image.

Plugins that call web services remain functionally unavailable offline even
when their Python dependencies are bundled.

## Migration Inventory

### Keep and port

- offline ARM64/AMD64 image and code-mount deployment;
- Python data, web, database, Oracle, Hive, Hadoop and browser runtime;
- authentication and per-user workspace/agent isolation;
- administrator user management and model/Skill/Tool permissions;
- local OpenAI-compatible provider and host/network access;
- generated-file delivery links and lifecycle cleanup;
- company knowledge base with asynchronous serial ingestion and multimodal
  document parsing;
- Chinese offline branding and removal of internet-only UI/features.

### Re-implement against v2 APIs

- file delivery integration with the v2 chat response/card model;
- knowledge-base retrieval injection into the v2 runtime prompt/tool layer;
- user-scoped provider/model/Skill/Tool filtering;
- multi-user auth around v2 workspaces, sessions and plugin state;
- code-mount entrypoint and static-console configuration.

### Do not port blindly

- old `@agentscope-ai/chat` pagination, push-message and scroll patches;
- old streaming/tool-call compatibility patches;
- old approval-card and safety-state patches;
- old session switching/new-chat workarounds;
- old session search implementation if it duplicates upstream history/archival
  behavior.

## Acceptance Order

1. Start the clean v2.0.1 ARM baseline with the existing offline base image.
2. Verify local OpenAI-compatible model chat and tool calls.
3. Verify long-chat browser scrolling independently from Scroll Context.
4. Port authentication and workspace isolation.
5. Port local model administration and per-user permissions.
6. Port file delivery and the knowledge base.
7. Remove internet-only UI and apply the Chinese branding.
8. Build and test the ARM code-mount package, then adapt the same changes to
   the AMD64 release.
