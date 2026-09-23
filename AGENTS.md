# Repository execution policy

## Local runtime and validation

- The local checkout is the source of truth for code, documentation, Git metadata, and uncommitted edits.
- Run project tests, dependency installation, the CLI, Web service, MinerU, Codex workers, and scientific reproductions locally. Remote access or synchronization is not a prerequisite for development or validation.
- Prefer an existing suitable Python environment. Keep test environments and generated case outputs outside the project checkout; use the configured case root for reproductions.
- Preserve existing case outputs, logs, checkpoints, and unrelated uncommitted edits.
- Store local development artifacts in the fixed directory `D:\geng-artifacts`, outside the checkout. Use `reports/` for test XML and validation reports, `logs/` for captured test/command output, `patches/` for temporary patch files, and `tmp/` for test scratch directories. Do not put these files or directories directly in a drive root. Use unique short run names to preserve previous results and avoid Windows path-length issues; point explicit `--junitxml`, log redirection, and `--basetemp` paths into these subdirectories. Scientific reproduction cases still use the configured case root.
- Keep temporary review snapshots and manually created development worktrees under this same artifact root (`tmp/` and `worktrees/` respectively). Move registered Git worktrees with `git worktree move`, preserving their state; leave Codex-managed worktrees in their app-managed locations. Keep previous artifacts unless cleanup is explicitly requested.
- The scripts under `tools/remote_*` remain optional utilities. Use a remote host only when explicitly requested; never copy SSH private keys, Codex credentials, virtual environments, caches, or `node_modules` as source artifacts.
