from __future__ import annotations

import time

from fastapi import Request

from .app_runtime import app
from .observability import logger


def _keep_spa_fallback_last() -> None:
    """Routes registered after app_v2 must stay ahead of the React catch-all."""
    fallbacks = [route for route in app.router.routes if getattr(route, "path", None) == "/{path:path}"]
    for route in fallbacks:
        app.router.routes.remove(route)
        app.router.routes.append(route)


_keep_spa_fallback_last()


@app.middleware("http")
async def structured_request_log(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    logger.info(
        "request completed",
        extra={
            "event": "http.request",
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    return response


__all__ = ["app"]
