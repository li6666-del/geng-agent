from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from threading import Lock, RLock
from typing import Any


_VOLATILE_FINGERPRINT_FIELDS = frozenset(
    {
        "fingerprint",
        "created_at",
        "updated_at",
        "timestamp",
        "observed_at",
        "last_seen_at",
    }
)

_LOCKS_GUARD = Lock()
_PATH_LOCKS: dict[str, RLock] = {}


class FailureMemoryFormatError(ValueError):
    """Raised when a JSONL failure-memory entry cannot be decoded."""

    def __init__(self, path: Path, line_number: int, message: str) -> None:
        super().__init__(f"{path}:{line_number}: {message}")
        self.path = path
        self.line_number = line_number


def failure_fingerprint(record: Mapping[str, Any]) -> str:
    """Return a deterministic SHA-256 identity for a failure record.

    Observation timestamps and a previously stored fingerprint do not define the
    failure itself. All other JSON content does, including nested key/value data.
    """

    if not isinstance(record, Mapping):
        raise TypeError("failure record must be a mapping")
    semantic_record = {
        str(key): value
        for key, value in record.items()
        if str(key) not in _VOLATILE_FINGERPRINT_FIELDS
    }
    try:
        encoded = json.dumps(
            semantic_record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"failure record must contain finite JSON values: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def normalize_failure(record: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a JSON-compatible record and attach its verified fingerprint."""

    fingerprint = failure_fingerprint(record)
    normalized = dict(record)
    normalized["fingerprint"] = fingerprint
    # Serialization here catches nested unsupported values before append can
    # leave a partially useful JSONL file behind.
    try:
        json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"failure record must contain finite JSON values: {exc}") from exc
    return normalized


def load_failures(path: str | Path, *, strict: bool = True) -> list[dict[str, Any]]:
    """Load and de-duplicate JSONL records, preserving first-seen order.

    Stored fingerprints are never trusted: each record is normalized again. In
    non-strict mode malformed lines and non-object JSON values are skipped.
    """

    memory_path = Path(path)
    if not memory_path.exists():
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    with memory_path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if not isinstance(parsed, dict):
                    raise ValueError("entry must be a JSON object")
                normalized = normalize_failure(parsed)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                if strict:
                    raise FailureMemoryFormatError(memory_path, line_number, str(exc)) from exc
                continue
            fingerprint = normalized["fingerprint"]
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            records.append(normalized)
    return records


def append_failure(path: str | Path, record: Mapping[str, Any]) -> bool:
    """Append a failure unless its fingerprint already exists.

    Returns ``True`` only when a new line was written. A per-path process lock
    keeps concurrent callers in this process from racing the load/append pair.
    """

    memory_path = Path(path)
    normalized = normalize_failure(record)
    lock = _path_lock(memory_path)
    with lock:
        existing = load_failures(memory_path)
        if any(item["fingerprint"] == normalized["fingerprint"] for item in existing):
            return False
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        prefix = "\n" if _needs_line_separator(memory_path) else ""
        with memory_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(prefix + payload + "\n")
        return True


def query_failures(
    source: str | Path | Iterable[Mapping[str, Any]],
    *,
    task: str | None = None,
    task_id: str | None = None,
    scenario: str | None = None,
) -> list[dict[str, Any]]:
    """Return failures matching an exact task and/or scenario.

    ``task`` is accepted as a query alias for ``task_id``. Records using either
    key are queryable so older failure logs can be migrated without rewriting.
    """

    if task is not None and task_id is not None and task != task_id:
        raise ValueError("task and task_id must match when both are provided")
    selected_task = task_id if task_id is not None else task
    if isinstance(source, (str, Path)):
        records = load_failures(source)
    else:
        records = _dedupe_records(source)
    return [
        record
        for record in records
        if (selected_task is None or _record_task(record) == selected_task)
        and (scenario is None or record.get("scenario") == scenario)
    ]


class FailureMemory:
    """Path-bound convenience API for a JSONL failure memory."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, record: Mapping[str, Any]) -> bool:
        return append_failure(self.path, record)

    def load(self, *, strict: bool = True) -> list[dict[str, Any]]:
        return load_failures(self.path, strict=strict)

    def query(
        self,
        *,
        task: str | None = None,
        task_id: str | None = None,
        scenario: str | None = None,
    ) -> list[dict[str, Any]]:
        return query_failures(self.path, task=task, task_id=task_id, scenario=scenario)


def _dedupe_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        normalized = normalize_failure(record)
        fingerprint = normalized["fingerprint"]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append(normalized)
    return result


def _record_task(record: Mapping[str, Any]) -> Any:
    return record.get("task_id", record.get("task"))


def _needs_line_separator(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open("rb") as handle:
        handle.seek(-1, 2)
        return handle.read(1) not in {b"\n", b"\r"}


def _path_lock(path: Path) -> RLock:
    key = str(path.expanduser().resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, RLock())
