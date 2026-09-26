"""Best-effort host observations; failure to save one never revokes a handoff."""
from collections import deque
from threading import RLock
from .outputs import write_json as _write_json
from .security import redact_text

_errors = deque(maxlen=200)
_lock = RLock()


def record_error(path, error):
    with _lock:
        _errors.append({"path": str(path), "error": redact_text(f"{type(error).__name__}: {error}")})


def observation_errors():
    with _lock:
        return list(_errors)


def write_json(path, value):
    try:
        _write_json(path, value)
    except Exception as exc:
        record_error(path, exc)
