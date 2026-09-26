from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from geng_agent.config import get_cases_root


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class WebSettings:
    cases_root: Path
    database_url: str
    redis_url: str
    max_pdf_bytes: int
    celery_eager: bool
    cookie_secure: bool
    session_days: int
    registration_enabled: bool
    execution_mode: str = "local"
    worker_token: str = ""
    max_bundle_bytes: int = 2 * 1024 * 1024 * 1024

    @classmethod
    def load(cls) -> "WebSettings":
        root = get_cases_root()
        database_url = os.getenv("GENG_DATABASE_URL") or f"sqlite:///{(root / 'geng_web.db').as_posix()}"
        return cls(
            cases_root=root,
            database_url=database_url,
            redis_url=os.getenv("GENG_REDIS_URL", "redis://127.0.0.1:6379/0"),
            max_pdf_bytes=int(os.getenv("GENG_MAX_PDF_BYTES", str(80 * 1024 * 1024))),
            celery_eager=_bool_env("GENG_CELERY_EAGER", default=database_url.startswith("sqlite")),
            cookie_secure=_bool_env("GENG_COOKIE_SECURE"),
            session_days=int(os.getenv("GENG_SESSION_DAYS", "7")),
            registration_enabled=_bool_env("GENG_REGISTRATION_ENABLED", default=True),
            execution_mode=os.getenv("GENG_EXECUTION_MODE", "local").strip().lower(),
            worker_token=os.getenv("GENG_WORKER_TOKEN", ""),
            max_bundle_bytes=int(os.getenv("GENG_MAX_BUNDLE_BYTES", str(2 * 1024 * 1024 * 1024))),
        )


settings = WebSettings.load()
