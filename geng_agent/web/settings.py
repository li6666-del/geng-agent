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
    enable_url_import: bool
    max_pdf_bytes: int
    celery_eager: bool

    @classmethod
    def load(cls) -> "WebSettings":
        root = get_cases_root()
        database_url = os.getenv("GENG_DATABASE_URL") or f"sqlite:///{(root / 'geng_web.db').as_posix()}"
        return cls(
            cases_root=root,
            database_url=database_url,
            redis_url=os.getenv("GENG_REDIS_URL", "redis://127.0.0.1:6379/0"),
            enable_url_import=_bool_env("GENG_ENABLE_URL_IMPORT"),
            max_pdf_bytes=int(os.getenv("GENG_MAX_PDF_BYTES", str(80 * 1024 * 1024))),
            celery_eager=_bool_env("GENG_CELERY_EAGER", default=database_url.startswith("sqlite")),
        )


settings = WebSettings.load()
