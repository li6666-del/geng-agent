"""Agentic science-repair backend (Codex CLI), the general-agent counterpart to the one-shot
per-file LLM rewrite in Phase D.

The blind rewrite regenerates files from a diagnosis and hopes; a coding agent can ITERATE
inside the project — run a task, print intermediates (Gram condition numbers, per-step SINR),
locate which step zeroed the signal, fix, re-run. That debugging loop is exactly what the
"physics -> code" wall needs.

The harness keeps every safety property it already has:
- agent proposes, harness disposes: the keep-only-if-improved gate, snapshot/restore and the
  re-run + re-review evaluation in run_science_repair are untouched;
- trusted files stay trusted: anything harness-owned the agent touches is deterministically
  re-injected afterwards (and the touch is recorded for audit);
- the static security scan + dependency-consistency gate still run on the next execution, so
  agent-written code passes the same checks as LLM-written code;
- the full agent transcript and the exact brief are persisted under audit/.

Never raises: any failure (CLI missing, timeout, non-zero exit) is reported in the returned
status dict and leaves the project for the normal evaluate -> no-improvement -> revert path.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import get_config_value
from .io_runtime import inject_io_runtime
from .outputs import write_json, write_text
from .result_review import summarize_csv_file
from .science_repair import build_science_directive, diagnose_csv_symptoms
from .security import redact_text
from .task_scripts import write_task_scaffolding


# Harness-owned files the agent must never effectively change. After the session they are
# restored deterministically (re-injected or byte-restored), regardless of what the agent did.
TRUSTED_PROJECT_FILES = (
    "src/_io.py",
    "run_experiment.py",
    "tasks_manifest.json",
    "tasks/__init__.py",
    "requirements.txt",
)

MAX_TRANSCRIPT_CHARS = 200_000


def collect_symptoms_by_task(
    repro_project_dir: Path, mismatches: list[dict[str, Any]], tasks_manifest: dict[str, Any]
) -> dict[str, list[str]]:
    """Paper-agnostic numeric symptoms for each mismatch task's output CSVs (same leads the
    LLM backend feeds into its directive), so the agent starts from 'column X is all-zero ->
    check normalization' instead of only prose."""
    subdir_by_task = {
        str(t.get("task_id")): str(t.get("output_subdir") or t.get("task_id"))
        for t in tasks_manifest.get("tasks", [])
        if isinstance(t, dict) and t.get("task_id")
    }
    symptoms: dict[str, list[str]] = {}
    for mismatch in mismatches:
        task_id = str(mismatch.get("task_id") or "")
        subdir = subdir_by_task.get(task_id, task_id)
        tips: list[str] = []
        for csv_path in sorted((repro_project_dir / "outputs" / subdir).glob("*.csv")):
            try:
                tips.extend(diagnose_csv_symptoms(summarize_csv_file(csv_path).get("numeric_columns", {})))
            except Exception:
                continue
        if tips:
            symptoms[task_id] = tips
    return symptoms


def build_agentic_repair_brief(
    *,
    mismatches: list[dict[str, Any]],
    symptoms_by_task: dict[str, list[str]],
    thesis_anchor: str,
    offending_scripts: list[str],
) -> str:
    """The standalone task brief handed to the coding agent. Self-contained: the diagnosis
    (review prose + numeric symptom leads), the paper-thesis anchor, how to run/verify inside
    the project, and the hard contract (what it may and may not touch)."""
    directive = build_science_directive(mismatches, symptoms_by_task)
    scripts = "、".join(offending_scripts) if offending_scripts else "（无任务脚本，仅 src/）"
    return (
        "# 任务：修复论文复现项目的科学问题（就在当前目录内迭代调试）\n\n"
        "你在一个通信论文复现项目的根目录。下面是结果审查给出的诊断——这些实验目前未能支持论文结论：\n\n"
        f"{directive}\n"
        f"{thesis_anchor}\n\n"
        "# 项目结构与运行方式\n"
        "- 共享科学计算在 src/（channel / modulation / metrics / simulation）；产物读写运行时在 src/_io.py（受信任，勿动）。\n"
        "- 每个实验一个薄驱动：tasks/<task_id>.py。复跑单个实验（在项目根目录）：\n"
        "  python -m tasks.<task_id> config_smoke.json\n"
        "- 产物落在 outputs/<task_id>/（results.csv、*.png、summary.json）。\n\n"
        "# 建议修法（迭代，而不是盲改）\n"
        "1. 先复跑涉事任务，读 outputs/ 里的 CSV，确认症状。\n"
        "2. 写少量临时打印/断言定位中间量（例如 Gram 矩阵条件数 cond(G) vs cond(Ḡ)、逐步 SINR、噪声方差取值），找到把结果带偏的那一步。\n"
        "3. 最小修改 src/ 与涉事任务脚本；每改一处就复跑验证，直到结果的相对排序/形状与上面的论文断言一致。\n"
        "4. 把临时打印清理掉，最后把全部任务各复跑一遍确认都能通过（退出码 0）。\n\n"
        "# 硬性契约（违反会被自动还原/拦截）\n"
        f"- 只允许修改：src/*.py（src/_io.py 除外）与这些任务脚本：{scripts}。\n"
        "- 绝不修改：src/_io.py、run_experiment.py、tasks_manifest.json、tasks/__init__.py、requirements.txt（这些是受信任文件，改了也会被确定性还原）。\n"
        "- 不要新增第三方依赖；不要联网；不要创建多余文件（临时脚本用完必须删除）。\n"
        "- 产物落盘只走 src/_io 的 begin / write_table / write_figure / finish，不要自己写 csv/json/savefig。\n"
        "- 生成代码禁止使用 subprocess、importlib、eval、getattr 动态分发（安全扫描会拦截整个项目）。\n"
        "- 绝不硬凑/伪造数值去贴论文（加偏置、裁剪、写死结果都不行）——要修的是建模本身；修好后排序应当自然成立。\n"
    )


def _split_command(raw: str) -> list[str]:
    """Split a command template that may contain Windows paths with spaces. posix=False keeps
    backslashes intact; surrounding quotes are then stripped per token."""
    return [token.strip('"') for token in shlex.split(raw, posix=False) if token.strip('"')]


def run_agentic_science_repair(
    *,
    repro_project_dir: Path,
    mismatches: list[dict[str, Any]],
    tasks_manifest: dict[str, Any],
    thesis_anchor: str,
    audit_dir: Path,
    timeout: float,
    codex_cmd: str | None = None,
    round_no: int = 1,
) -> dict[str, Any]:
    """Run one agentic repair session over the project, then deterministically restore every
    trusted file. Returns a status dict; never raises (a failed session leaves the project to
    the normal evaluate -> revert path of run_science_repair)."""
    label = f"06_agentic_repair_codex_round_{round_no:02d}"
    script_by_task_id = {
        str(t.get("task_id")): str(t.get("script"))
        for t in tasks_manifest.get("tasks", [])
        if isinstance(t, dict) and t.get("task_id") and t.get("script")
    }
    offending_scripts = [
        script_by_task_id[m["task_id"]]
        for m in mismatches
        if m.get("task_id") in script_by_task_id
    ]
    symptoms = collect_symptoms_by_task(repro_project_dir, mismatches, tasks_manifest)
    brief = build_agentic_repair_brief(
        mismatches=mismatches,
        symptoms_by_task=symptoms,
        thesis_anchor=thesis_anchor,
        offending_scripts=offending_scripts,
    )
    write_text(audit_dir / f"{label}_brief.md", brief)

    # Snapshot trusted-file bytes so we can both DETECT a touch (audit) and RESTORE it.
    trusted_before: dict[str, bytes | None] = {}
    for rel in TRUSTED_PROJECT_FILES:
        path = repro_project_dir / rel
        trusted_before[rel] = path.read_bytes() if path.exists() else None

    raw_cmd = codex_cmd or get_config_value("GENG_CODEX_CMD") or "codex"
    argv = _split_command(raw_cmd)
    resolved = shutil.which(argv[0]) if argv else None
    status: dict[str, Any] = {
        "ok": False,
        "backend": "codex",
        "round": round_no,
        "command": None,
        "returncode": None,
        "timed_out": False,
        "error": None,
        "touched_trusted": [],
        "transcript": None,
        "duration_s": None,
    }
    if not argv or resolved is None:
        status["error"] = f"codex CLI not found: {raw_cmd!r} (install it or set GENG_CODEX_CMD)"
        write_json(audit_dir / f"{label}.json", status)
        return status

    # --skip-git-repo-check: the repro project is intentionally NOT a git repo, and codex
    # refuses untrusted non-repo dirs otherwise (observed live on codex-cli 0.133).
    command = [
        resolved,
        *argv[1:],
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(repro_project_dir),
        brief,
    ]
    status["command"] = command[:-1] + ["<brief omitted>"]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=repro_project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            # codex reads "additional input" from stdin when it is not a tty; DEVNULL gives it
            # an immediate EOF instead of a hang in headless runs.
            stdin=subprocess.DEVNULL,
        )
        status["returncode"] = completed.returncode
        status["ok"] = completed.returncode == 0
        transcript = (completed.stdout or "") + ("\n--- stderr ---\n" + completed.stderr if completed.stderr else "")
    except subprocess.TimeoutExpired as exc:
        status["timed_out"] = True
        status["error"] = f"agent session timed out after {timeout:.0f}s"
        out = exc.stdout or b""
        err = exc.stderr or b""
        transcript = (out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)) + (
            "\n--- stderr ---\n" + (err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err))
        )
    except Exception as exc:  # never raise: a broken backend must not sink the pipeline
        status["error"] = f"{type(exc).__name__}: {exc}"
        transcript = ""
    status["duration_s"] = round(time.monotonic() - started, 1)

    transcript_path = audit_dir / f"{label}_transcript.txt"
    write_text(transcript_path, redact_text(transcript)[-MAX_TRANSCRIPT_CHARS:])
    status["transcript"] = str(transcript_path)

    # Trusted-file guard: detect what the agent touched, then restore DETERMINISTICALLY —
    # requirements.txt from saved bytes, src/_io.py via inject_io_runtime, and the dispatcher /
    # tasks/__init__.py / tasks_manifest.json via write_task_scaffolding.
    for rel, before in trusted_before.items():
        path = repro_project_dir / rel
        after = path.read_bytes() if path.exists() else None
        if after != before:
            status["touched_trusted"].append(rel)
    if status["touched_trusted"]:
        requirements_before = trusted_before.get("requirements.txt")
        if requirements_before is not None:
            (repro_project_dir / "requirements.txt").write_bytes(requirements_before)
    inject_io_runtime(repro_project_dir)
    if tasks_manifest.get("tasks"):
        write_task_scaffolding(repro_project_dir, tasks_manifest)

    write_json(audit_dir / f"{label}.json", status)
    return status
