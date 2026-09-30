"""Weekly cleanup of expired website cases; the CLI defaults to a dry run.

The server authorizes specific completed job generations. Manual cases and
unacknowledged local failures are never inferred to be expired from local age.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import psutil

from geng_agent.artifact_paths import path_is_link

from .local_worker import (LocalWorker, WorkerConfig, WorkerError, _atomic_json,
                           _read_json, _safe_local_path, worker_lock)

LOG = logging.getLogger("geng_agent.local_retention")
FAILED_RETRY = timedelta(hours=1)
BEIJING = timezone(timedelta(hours=8))


def normalize_generation(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WorkerError("Local case generation must be a UUID") from exc


def local_case_name(case_id: str, generation: str | None) -> str:
    """Keep existing cases unchanged; a cleared case gets a fresh short root."""
    try:
        normalized_case = str(uuid.UUID(case_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WorkerError("Cloud case identifier must be a UUID") from exc
    return f"cloud_{normalize_generation(generation) or normalized_case}"


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _remove_tree(path: Path, root: Path) -> None:
    """Unlink junction entries, never recurse into their shared targets."""
    path.relative_to(root)
    if path_is_link(path):
        if path.is_symlink():
            path.unlink()
        else:
            # Windows directory junctions must be removed with rmdir.
            path.rmdir()
        return
    if path.is_dir():
        with os.scandir(path) as entries:
            children = [Path(entry.path) for entry in entries]
        for child in children:
            _remove_tree(child, root)
        path.rmdir()
    elif path.exists():
        try:
            path.unlink()
        except PermissionError:
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
            path.unlink()


def _case_has_live_process(directory: Path) -> bool:
    """Detect manual resumes and leftover science without logging their arguments."""
    normalized = os.path.normcase(str(directory.resolve())).replace("\\", "/").rstrip("/")
    pattern = re.compile(r"(?<![\w./-])" + re.escape(normalized) + r"(?=$|[/\s\"'=,;\)\]\}])")
    for process in psutil.process_iter(["pid", "cwd", "cmdline", "status"], ad_value=None):
        try:
            info = process.info
            if info["pid"] == os.getpid() or info.get("status") in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
                continue
            cwd = info.get("cwd")
            if cwd:
                current = os.path.normcase(str(Path(cwd).resolve())).replace("\\", "/").rstrip("/")
                if current == normalized or current.startswith(normalized + "/"):
                    return True
            for argument in info.get("cmdline") or []:
                text = os.path.normcase(str(argument)).replace("\\", "/")
                if pattern.search(re.sub(r"/{2,}", "/", text)):
                    return True
        except (psutil.Error, OSError):
            # Inaccessible system processes do not hold every website case.
            continue
    return False


def _clean_trash(worker: LocalWorker, *, apply: bool) -> int:
    trash = _safe_local_path(worker.config.case_root, ".cloud-trash")
    if not trash.is_dir():
        return 0
    count = 0
    for marker in sorted(trash.glob("*.json")):
        if path_is_link(marker):
            continue
        try:
            record = _read_json(marker)
            # Only our previously authorized rename records are deletion roots.
            if record.get("worker_id") != worker.worker_id or record.get("origin") != worker.config.url:
                continue
            folder = record.get("folder")
            if not isinstance(folder, str) or folder != marker.stem or not uuid.UUID(folder):
                continue
            target = _safe_local_path(trash, folder)
            original_name = local_case_name(record["case_id"], record.get("local_generation"))
            if record.get("original_name", original_name) != original_name:
                continue
            original = _safe_local_path(worker.config.case_root, original_name)
            if _case_has_live_process(original) or _case_has_live_process(target):
                LOG.info("Cleanup staging is still referenced by a live process; retained")
                continue
            if apply:
                _remove_tree(target, trash)
                marker.unlink()
            count += 1
        except (KeyError, OSError, ValueError, WorkerError):
            LOG.warning("An authorized cleanup directory is still retained; retry on the next maintenance run")
    return count


def cleanup_cases(worker: LocalWorker, *, apply: bool = False) -> dict[str, Any]:
    """Caller owns worker_lock; one server request, no endless transfer retry."""
    response = worker._request("/api/v1/worker/cleanup-candidates", {"worker_id": worker.worker_id})
    candidates = response.get("items")
    if not isinstance(candidates, list):
        raise WorkerError("Cloud cleanup response has no candidate list")
    removed: list[str] = []
    skipped = 0
    resumed = _clean_trash(worker, apply=apply)
    visited: set[str] = set()
    for item in candidates:
        try:
            case_id = str(uuid.UUID(item["case_id"]))
            job_id = str(uuid.UUID(item["job_id"]))
            generation = normalize_generation(item.get("local_generation"))
            if _timestamp(item.get("expired_at")) is None:
                raise WorkerError("Cloud cleanup candidate has no expiration time")
            name = local_case_name(case_id, generation)
            if name in visited:
                continue
            target = _safe_local_path(worker.config.case_root, name)
            state_path = _safe_local_path(target, ".cloud-worker.json")
            if not target.is_dir() or not state_path.is_file():
                skipped += 1
                continue
            state = _read_json(state_path)
            if (state.get("case_id") != case_id or state.get("job_id") != job_id
                    or state.get("local_generation") != generation):
                skipped += 1
                continue
            if _case_has_live_process(target):
                skipped += 1
                LOG.info("Expired case %s is still referenced by a live process; retained", name)
                continue
            if apply:
                trash = _safe_local_path(worker.config.case_root, ".cloud-trash")
                trash.mkdir(exist_ok=True)
                folder = str(uuid.uuid4())
                moved = _safe_local_path(trash, folder)
                marker = _safe_local_path(trash, folder + ".json")
                _atomic_json(marker, {"origin": worker.config.url, "worker_id": worker.worker_id,
                                      "folder": folder, "original_name": name, "case_id": case_id, "job_id": job_id,
                                      "local_generation": generation, "expired_at": item["expired_at"]})
                # Persist authorization before rename, so interrupted deletion
                # can resume even after .cloud-worker.json has been removed.
                target.rename(moved)
                try:
                    _remove_tree(moved, trash)
                    marker.unlink()
                except OSError:
                    LOG.warning("Expired case %s moved to cleanup staging; deletion will resume later", name)
            visited.add(name)
            removed.append(name)
        except (KeyError, ValueError, TypeError, AttributeError, OSError, WorkerError):
            skipped += 1
            LOG.warning("A cleanup candidate was retained because its local directory could not be matched")
    return {"mode": "apply" if apply else "dry_run", "cases": removed,
            "skipped": skipped, "resumed_staging": resumed}


def _maintenance_path(worker: LocalWorker) -> Path:
    key = uuid.uuid5(uuid.NAMESPACE_URL, worker.config.url).hex[:16]
    return _safe_local_path(worker.config.case_root, f".cloud-worker/cleanup-{key}.json")


def _weekly_window(now: datetime) -> datetime:
    """Most recent Monday 04:10 in Beijing, after server weekly expiry."""
    local = now.astimezone(BEIJING)
    window = (local - timedelta(days=local.weekday())).replace(hour=4, minute=10, second=0, microsecond=0)
    if window > local:
        window -= timedelta(days=7)
    return window.astimezone(timezone.utc)


def _run_cleanup(worker: LocalWorker, now: datetime) -> dict[str, Any]:
    path = _maintenance_path(worker)
    saved = _read_json(path) if path.exists() else {}
    saved["last_attempt"] = now.isoformat()
    _atomic_json(path, saved)
    result = cleanup_cases(worker, apply=True)
    saved["last_success"] = now.isoformat()
    saved["last_result"] = result
    _atomic_json(path, saved)
    LOG.info("Weekly cleanup: %d expired cases, %d staging directories", len(result["cases"]), result["resumed_staging"])
    return result


def cleanup_if_due(worker: LocalWorker, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    path = _maintenance_path(worker)
    try:
        saved = _read_json(path) if path.exists() else {}
        last_success = _timestamp(saved.get("last_success"))
        last_attempt = _timestamp(saved.get("last_attempt"))
        if last_success and last_success >= _weekly_window(now):
            return False
        if last_attempt and now - last_attempt < FAILED_RETRY:
            return False
        _run_cleanup(worker, now)
        return True
    except (httpx.TransportError, WorkerError, OSError):
        # Unsupported old servers, HTTP errors and storage faults must not stop
        # claiming or reproducing papers. Next maintenance attempt is bounded.
        LOG.warning("Weekly cleanup unavailable; paper processing continues")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--apply", action="store_true", help="Delete only server-authorized expired website cases")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    worker = None
    try:
        config = WorkerConfig.load(args.config)
        os.environ.pop("GENG_WORKER_TOKEN", None)
        try:
            with worker_lock(config.case_root):
                worker = LocalWorker(config)
                if args.apply:
                    # A weekly task invocation is explicit: execute even if an
                    # earlier empty maintenance pass already wrote a marker.
                    _run_cleanup(worker, datetime.now(timezone.utc))
                else:
                    print(json.dumps(cleanup_cases(worker), ensure_ascii=False))
        except WorkerError as exc:
            if "already using" in str(exc):
                LOG.info("Local worker is active; it performs weekly cleanup between papers")
                return 0
            raise
        return 0
    except (WorkerError, httpx.TransportError, OSError):
        LOG.error("Cleanup could not be completed; all unmatched case files remain retained")
        return 1
    finally:
        if worker is not None:
            worker.close()


if __name__ == "__main__":
    raise SystemExit(main())
