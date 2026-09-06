# Optional remote execution tools

The local Windows checkout is the source of truth and the default execution and validation environment. Remote access is not required. This page documents optional tools retained for an explicitly requested remote run; it is not a repository execution policy.

## Locations

- SSH alias: `geng-agent-remote`
- Remote project: `/root/geng-agent-task-driven`
- Remote Python: `/root/.venvs/geng-agent/bin/python`
- Remote cases: `/root/geng-agent-cases`
- Previous project mirror: `/root/geng-agent-task-driven.previous`

## Synchronize local changes

```powershell
powershell -ExecutionPolicy Bypass -File tools/remote_sync.ps1
```

The synchronizer includes `.git`, tracked edits, and untracked source files. It excludes local caches, virtual environments, compiled Python files, and `node_modules`. The current remote mirror is retained as `.previous` for one-step recovery. Case outputs live outside the mirror and are not touched.

Once the remote Node runtime exists, every successful sync also runs `npm ci`
and rebuilds the production Web bundle on the remote host. Generated frontend
files therefore come from the remote execution environment, even though the
Windows checkout remains the source of truth for source code.

## Observe the remote host

```powershell
powershell -ExecutionPolicy Bypass -File tools/remote_status.ps1
```

The status command is read-only. It reports the mirror state, installed runtime,
Codex authentication, GPU/storage usage, active project processes, and the last
successful sync checksum.

## Rebuild the remote environment

```bash
ssh geng-agent-remote
bash /root/geng-agent-task-driven/tools/remote_bootstrap.sh
```

The bootstrap installs an isolated Python 3.11 environment, the CUDA PyTorch wheel, all `repro` and `web` extras, test tooling, Node.js LTS, frontend packages, and the official standalone Codex CLI. It does not copy Codex credentials from Windows.

On this AutoDL/SeetaCloud image, the generated environment also sources
`/etc/network_turbo` when available so Codex can reach OpenAI without routing
through the Windows machine.

If the standalone Codex endpoint is unavailable from the remote network, the
bootstrap falls back to OpenAI's official `@openai/codex` npm package.

## Run commands remotely

```powershell
ssh geng-agent-remote
cd /root/geng-agent-task-driven
export GENG_CASES_ROOT=/root/geng-agent-cases
/root/.venvs/geng-agent/bin/python -m geng_agent doctor
/root/.venvs/geng-agent/bin/python -m pytest -q
```

Start the Web UI on the remote loopback interface:

```bash
bash /root/geng-agent-task-driven/tools/remote_web.sh start
```

Observe it from Windows through an SSH tunnel without exposing the service to
the public Internet:

```powershell
ssh -N -L 8765:127.0.0.1:8765 geng-agent-remote
```

Then open `http://127.0.0.1:8765`. Other Web controls are
`remote_web.sh status`, `remote_web.sh log`, and `remote_web.sh stop`.

For a reproduction run:

```bash
export GENG_CASES_ROOT=/root/geng-agent-cases
/root/.venvs/geng-agent/bin/python -m geng_agent review /root/papers/paper.pdf --out case_001 --run-repro
```

Codex authentication is intentionally not copied from the Windows machine. Authenticate the remote CLI independently using the approved ChatGPT device-code or API-key flow.
