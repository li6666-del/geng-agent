from __future__ import annotations

from typing import Any


def create_app():
    from .app import create_app as factory

    return factory()


def __getattr__(name: str) -> Any:
    if name == "app":
        from .app import app

        return app
    raise AttributeError(name)


__all__ = ["app", "create_app"]
