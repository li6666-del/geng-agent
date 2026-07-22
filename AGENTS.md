# Repository execution policy

## Remote-first runtime

- The local checkout is the source of truth for code, documentation, Git metadata, and uncommitted edits.
- Run project tests, dependency installation, the CLI, Web service, MinerU, Codex workers, and scientific reproductions on `geng-agent-remote` unless the user explicitly requests a local run.
- The remote project root is `/root/geng-agent-task-driven`.
- The remote Python environment is `/root/.venvs/geng-agent`.
- The remote case root is `/root/geng-agent-cases`; never place case outputs inside the project checkout.
- Synchronize local edits with `powershell -ExecutionPolicy Bypass -File tools/remote_sync.ps1` before remote execution.
- Observe the remote host with `powershell -ExecutionPolicy Bypass -File tools/remote_status.ps1`.
- Do not copy local SSH private keys, Codex credentials, virtual environments, caches, or `node_modules` into the remote project.
- Treat the remote project checkout as an execution mirror. Make durable source edits locally, then synchronize them; preserve remote case outputs and logs when refreshing the project mirror.
