"""Public entrypoint for the durable voyage API."""

from fastapi import FastAPI

from geng_agent.web.app_runtime_logged import app


def create_app() -> FastAPI:
    return app


__all__ = ["app", "create_app"]
