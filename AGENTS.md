# Repository execution policy

## Local runtime and validation

- The local checkout is the source of truth for code, documentation, Git metadata, and uncommitted edits.
- Run project tests, dependency installation, the CLI, Web service, MinerU, Codex workers, and scientific reproductions locally. Remote access or synchronization is not a prerequisite for development or validation.
- Prefer an existing suitable Python environment. Keep test environments and generated case outputs outside the project checkout; use the configured case root for reproductions.
- Preserve existing case outputs, logs, checkpoints, and unrelated uncommitted edits.
- The scripts under `tools/remote_*` remain optional utilities. Use a remote host only when explicitly requested; never copy SSH private keys, Codex credentials, virtual environments, caches, or `node_modules` as source artifacts.
