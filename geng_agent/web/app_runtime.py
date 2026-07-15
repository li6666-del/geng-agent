from __future__ import annotations

import asyncio
import json

from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .app_v2 import _authorized_case, app
from .db import SessionLocal, get_session
from .events import event_cursor, events_after
from .models import JobRecord
from .settings import settings


@app.get("/api/v1/jobs/{job_id}/events/live")
async def stream_live_events(
    job_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    # The stream may live for hours, so authorize with a short-lived session.
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        _authorized_case(session, job.case_id)
    cursor = event_cursor(after, last_event_id)

    async def generate():
        nonlocal cursor
        pubsub = None
        redis_client = None
        try:
            try:
                import redis.asyncio as redis_async

                redis_client = redis_async.Redis.from_url(
                    settings.redis_url,
                    socket_connect_timeout=0.5,
                    socket_timeout=2,
                )
                pubsub = redis_client.pubsub()
                await pubsub.subscribe(f"geng:job:{job_id}")
            except Exception:
                if pubsub is not None:
                    await pubsub.aclose()
                if redis_client is not None:
                    await redis_client.aclose()
                pubsub = None
                redis_client = None

            idle = 0
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(events_after, job_id, cursor)
                if batch:
                    idle = 0
                    for event in batch:
                        cursor = int(event["id"])
                        yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    continue

                idle += 1
                if idle >= 15:
                    idle = 0
                    yield ": keep-alive\n\n"
                if pubsub is None:
                    await asyncio.sleep(1)
                else:
                    try:
                        await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    except Exception:
                        await pubsub.aclose()
                        pubsub = None
        finally:
            if pubsub is not None:
                await pubsub.aclose()
            if redis_client is not None:
                await redis_client.aclose()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/metrics", response_class=PlainTextResponse)
def prometheus_metrics(session: Session = Depends(get_session)) -> str:
    status_counts = dict(session.execute(select(JobRecord.status, func.count()).group_by(JobRecord.status)).all())
    finished = session.scalars(
        select(JobRecord).where(JobRecord.started_at.is_not(None), JobRecord.finished_at.is_not(None))
    ).all()
    durations = [(job.finished_at - job.started_at).total_seconds() for job in finished if job.finished_at and job.started_at]
    queue_depth = -1
    try:
        import redis

        queue_depth = int(redis.Redis.from_url(settings.redis_url, socket_connect_timeout=0.3).llen("celery"))
    except Exception:
        pass
    lines = [
        "# HELP geng_queue_depth Number of tasks waiting in the Celery queue.",
        "# TYPE geng_queue_depth gauge",
        f"geng_queue_depth {queue_depth}",
        "# HELP geng_job_total Jobs grouped by durable status.",
        "# TYPE geng_job_total gauge",
    ]
    for status, count in sorted(status_counts.items()):
        lines.append(f'geng_job_total{{status="{status}"}} {count}')
    lines.extend(
        [
            "# HELP geng_job_duration_seconds Average duration of completed jobs.",
            "# TYPE geng_job_duration_seconds gauge",
            f"geng_job_duration_seconds {sum(durations) / len(durations) if durations else 0:.3f}",
        ]
    )
    return "\n".join(lines) + "\n"


__all__ = ["app"]
