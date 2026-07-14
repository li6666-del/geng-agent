from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .codex_runner import split_command
from .config import get_config_value
from .mineru_adapter import build_figure_index, file_sha256
from .outputs import write_json, write_text
from .security import codex_safe_env, redact_text


MINERU_INDEX_FILE = "paper_figure_index.json"
MAX_TRANSCRIPT_CHARS = 200_000
MINERU_FIGURE_LOCATOR_ARGS = ["--formula", "false", "--table", "false"]


def run_mineru_layout_stage(
    *,
    paper_path: Path,
    output_dir: Path,
    audit_dir: Path,
    resume: bool,
    timeout: float = 1800.0,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """Run MinerU once per PDF and fail open to the existing page-image path."""
    stage_dir = audit_dir / "00_mineru"
    raw_dir = stage_dir / "raw"
    candidate_dir = stage_dir / "candidates"
    status_path = stage_dir / "mineru_status.json"
    index_path = output_dir / MINERU_INDEX_FILE
    stage_dir.mkdir(parents=True, exist_ok=True)
    paper_hash = file_sha256(paper_path) if paper_path.is_file() else ""
    raw_command = get_config_value("GENG_MINERU_CMD") or "mineru"
    backend = get_config_value("GENG_MINERU_BACKEND")
    cache_root = get_config_value("GENG_MINERU_CACHE_ROOT")
    argv = split_command(raw_command)
    resolved = shutil.which(argv[0]) if argv else None
    page_args = ["-e", str(max_pages - 1)] if isinstance(max_pages, int) and max_pages > 0 else []
    stage_args = [*MINERU_FIGURE_LOCATOR_ARGS, *page_args]
    input_hash = _input_hash(
        paper_hash=paper_hash,
        command=raw_command,
        backend=backend,
        command_fingerprint=_command_fingerprint(argv, resolved),
        stage_args=stage_args,
    )

    if resume:
        cached = _load_cache(status_path=status_path, index_path=index_path, input_hash=input_hash)
        if cached is not None:
            cached["cached"] = True
            return cached

    if paper_path.suffix.lower() != ".pdf":
        return _write_unavailable(
            status_path=status_path,
            index_path=index_path,
            input_hash=input_hash,
            paper_hash=paper_hash,
            backend=backend,
            reason="MinerU layout parsing applies only to PDF inputs.",
            error_kind="unsupported_input",
        )

    if not argv or resolved is None:
        return _write_unavailable(
            status_path=status_path,
            index_path=index_path,
            input_hash=input_hash,
            paper_hash=paper_hash,
            backend=backend,
            reason=f"MinerU CLI not found: {raw_command!r}",
            error_kind="missing_cli",
        )

    running_status = {
        "ok": False,
        "state": "running",
        "available": True,
        "cached": False,
        "fallback_used": False,
        "error_kind": None,
        "error": None,
        "input_hash": input_hash,
        "paper_sha256": paper_hash,
        "backend": backend,
        "cache_root": cache_root,
        "command": None,
        "returncode": None,
        "timed_out": False,
        "duration_s": 0.0,
        "figure_count": 0,
    }
    write_json(status_path, running_status)
    index_path.unlink(missing_ok=True)
    for path in (raw_dir, candidate_dir):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
    command = [
        resolved,
        *argv[1:],
        "-p",
        str(paper_path),
        "-o",
        str(raw_dir),
        *stage_args,
    ]
    if backend:
        command.extend(["-b", backend])
    started = time.monotonic()
    transcript = ""
    returncode: int | None = None
    timed_out = False
    error: str | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=stage_dir,
            env=_mineru_env(cache_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        try:
            stdout, stderr = process.communicate(timeout=max(1.0, float(timeout or 1800.0)))
        except subprocess.TimeoutExpired:
            timed_out = True
            error = f"MinerU timed out after {timeout:.0f}s"
            _terminate_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
        returncode = process.returncode
        transcript = (stdout or "") + ("\n--- stderr ---\n" + stderr if stderr else "")
        if not timed_out and returncode != 0:
            error = f"MinerU exited with status {returncode}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    duration = round(time.monotonic() - started, 3)
    transcript_path = stage_dir / "mineru_transcript.txt"
    write_text(transcript_path, redact_text(transcript)[-MAX_TRANSCRIPT_CHARS:])

    figure_index = build_figure_index(
        raw_dir=raw_dir,
        paper_path=paper_path,
        case_root=output_dir,
        candidate_dir=candidate_dir,
        paper_sha256=paper_hash,
        backend=backend,
    )
    write_json(index_path, figure_index)
    figure_count = len(figure_index.get("figures", []))
    process_ok = returncode == 0 and not timed_out and error is None
    status = {
        "ok": process_ok and figure_count > 0,
        "state": "complete" if process_ok else "failed",
        "available": True,
        "cached": False,
        "fallback_used": not (process_ok and figure_count > 0),
        "error_kind": None if process_ok else ("timeout" if timed_out else "nonzero_exit" if returncode else "subprocess_error"),
        "error": error if error else (None if figure_count else "MinerU produced no captioned figure candidates."),
        "input_hash": input_hash,
        "paper_sha256": paper_hash,
        "backend": backend,
        "cache_root": cache_root,
        "command": command,
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_s": duration,
        "transcript": str(transcript_path),
        "raw_dir": str(raw_dir),
        "index_path": str(index_path),
        "figure_count": figure_count,
        "unmatched_visual_count": len(figure_index.get("unmatched_visuals", [])),
        "figure_index": figure_index,
    }
    write_json(status_path, status)
    return status


def _write_unavailable(
    *,
    status_path: Path,
    index_path: Path,
    input_hash: str,
    paper_hash: str,
    backend: str | None,
    reason: str,
    error_kind: str,
) -> dict[str, Any]:
    figure_index = {
        "schema_version": "1.0",
        "paper_sha256": paper_hash,
        "backend": backend,
        "source_format": "none",
        "figures": [],
        "unmatched_visuals": [],
        "_meta": {"figure_count": 0, "unmatched_visual_count": 0, "coordinate_system": "normalized_page_xyxy_0_1"},
    }
    write_json(index_path, figure_index)
    status = {
        "ok": False,
        "state": "unavailable",
        "available": False,
        "cached": False,
        "fallback_used": True,
        "error_kind": error_kind,
        "error": reason,
        "input_hash": input_hash,
        "paper_sha256": paper_hash,
        "backend": backend,
        "command": None,
        "returncode": None,
        "timed_out": False,
        "duration_s": 0.0,
        "transcript": None,
        "raw_dir": None,
        "index_path": str(index_path),
        "figure_count": 0,
        "unmatched_visual_count": 0,
        "figure_index": figure_index,
    }
    write_json(status_path, status)
    return status


def _mineru_env(cache_root: str | None) -> dict[str, str]:
    env = codex_safe_env()
    env["PYTHONNOUSERSITE"] = "1"
    if not cache_root:
        return env
    root = Path(cache_root).expanduser().resolve()
    mappings = {
        "HF_HOME": root / "huggingface",
        "MODELSCOPE_CACHE": root / "modelscope",
        "TORCH_HOME": root / "torch",
    }
    for name, path in mappings.items():
        path.mkdir(parents=True, exist_ok=True)
        env[name] = str(path)
    return env


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        process.kill()


def _load_cache(*, status_path: Path, index_path: Path, input_hash: str) -> dict[str, Any] | None:
    status = _read_json_object(status_path)
    index = _read_json_object(index_path)
    if (
        status.get("state") != "complete"
        or status.get("returncode") != 0
        or status.get("timed_out")
        or status.get("input_hash") != input_hash
        or index.get("paper_sha256") != status.get("paper_sha256")
    ):
        return None
    if not isinstance(index.get("figures"), list):
        return None
    if not _cache_assets_exist(index, index_path.parent):
        return None
    status["figure_index"] = index
    return status


def _cache_assets_exist(index: dict[str, Any], case_root: Path) -> bool:
    for item in index.get("figures", []):
        if not isinstance(item, dict):
            return False
        raw = str(item.get("asset_path") or "").strip()
        if not raw:
            continue
        path = (case_root / raw).resolve()
        try:
            inside = path.is_relative_to(case_root.resolve())
        except (OSError, ValueError):
            inside = False
        if not inside or not path.is_file() or path.is_symlink():
            return False
    return True


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 20_000_000:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _command_fingerprint(argv: list[str], resolved: str | None) -> list[dict[str, Any]]:
    fingerprints: list[dict[str, Any]] = []
    paths = [Path(resolved)] if resolved else []
    paths.extend(Path(token) for token in argv[1:] if Path(token).is_file())
    for path in paths:
        try:
            stat = path.stat()
            fingerprints.append(
                {
                    "path": str(path.resolve()),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        except OSError:
            fingerprints.append({"path": str(path), "missing": True})
    return fingerprints or [{"command": argv[0] if argv else None, "missing": resolved is None}]


def _input_hash(
    *,
    paper_hash: str,
    command: str,
    backend: str | None,
    command_fingerprint: list[dict[str, Any]],
    stage_args: list[str],
) -> str:
    import hashlib

    payload = json.dumps(
        {
            "paper_sha256": paper_hash,
            "command": command,
            "command_fingerprint": command_fingerprint,
            "stage_args": stage_args,
            "backend": backend,
            "adapter_version": "1.2",
        },
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
