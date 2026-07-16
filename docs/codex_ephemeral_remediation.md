# Codex Ephemeral Worker Remediation

Date: 2026-07-16

Worktree: the task-driven sibling worktree on `codex/integrate-frank-web`.

## Root cause

The project uses one-shot `codex exec` processes for analysis, task writing,
per-task reporting, and final report editing. Their durable evidence already
lives in project-owned last-message, transcript, JSON, and case artifacts, but
the centralized runner did not request ephemeral sessions. Codex CLI therefore
could also persist these workers in the user's personal desktop session history.

All current production call sites use
`geng_agent.codex_runner.run_codex_subprocess`:

- `geng_agent/agentic_analysis.py`
- `geng_agent/agentic_task_writers.py`
- `geng_agent/agentic_task_reporters.py`
- `geng_agent/agentic_report_editor.py`

No production `codex resume`, session-ID continuation, or `--last` path exists.

## Changes

- Every current worker command is now `codex exec --ephemeral ...`.
- Duplicate `--ephemeral` arguments from custom command configuration are
  normalized so the final worker command contains exactly one instance.
- Audit status records `session_persistence: ephemeral` and the capability
  probe result.
- The first worker performs a five-second `codex exec --help` capability probe.
  The result is process-local, cached, and single-flight across concurrent
  workers.
- A CLI without `--ephemeral` fails closed with
  `error_kind: unsupported_cli_feature`; it never falls back to persistent mode.
- Fake Codex fixtures now advertise and accept ephemeral execution.
- A static test rejects direct Codex CLI subprocess calls outside the
  centralized runner.

Changed files:

- `geng_agent/codex_runner.py`
- `tests/test_codex_runner.py`
- `tests/test_agentic_analysis.py`
- `tests/test_agentic_task_reporters.py`
- `tests/test_agentic_report_editor.py`
- `docs/codex_ephemeral_remediation.md`

## Persistence boundary

Current workers are safe to make ephemeral because the project does not depend
on Codex session persistence for continuation. A future resume feature requires
a separate design with case-local subprocess environment variables, an explicit
session UUID, and no `--last`. `CODEX_SQLITE_HOME` alone does not isolate all
sessions, logs, config, or authentication; zero writes to personal Codex data
would also require a separately provisioned `CODEX_HOME` and authentication
flow. No global environment or Codex configuration is changed here.

## Verification

- `python -m unittest tests.test_codex_runner`: 8 tests passed.
- Runner plus four caller suites: 27 tests passed.
- `python -m compileall geng_agent tests`: passed.
- `python -m unittest`: 287 tests passed.
- `git diff --check`: passed.
- Local CLI: `codex-cli 0.144.1`.
- `codex exec --help` advertises `--ephemeral`.
- Parallel fixture: one capability probe; overlapping worker subprocesses with
  peak concurrency of at least two.

## Global-state statement and remaining risk

No file under the user's global `.codex` directory was modified, cleaned,
renamed, or migrated. No real Codex worker smoke was run because it would consume
quota and was not explicitly authorized. The behavior is covered by mocked
subprocess tests and a read-only CLI help capability check.
