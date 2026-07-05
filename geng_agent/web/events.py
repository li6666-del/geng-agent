from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from .db import SessionLocal
from .models import JobEvent, JobRecord
from .settings import settings


def append_event(job_id: str, payload: dict[str, Any]) -> int:
    with SessionLocal() as session:
        job = session.get(JobRecord, job_id)
        if job is None:
            raise KeyError(job_id)
        event = JobEvent(
            job_id=job_id,
            event_type=str(payload.get("type") or "progress"),
            phase=payload.get("phase"),
            step=payload.get("step"),
            message=payload.get("message"),
            data=payload.get("data") if isinstance(payload.get("data"), dict) else {},
        )
        if event.event_type == "phase.started":
            job.current_phase = event.phase
            job.current_step = None
        elif event.step:
            job.current_step = event.step
        session.add(event)
        session.commit()
        session.refresh(event)
        event_id = int(event.id)
    _publish(job_id, event_id)
    return event_id


def _publish(job_id: str, event_id: int) -> None:
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=0.2, socket_timeout=0.2)
        client.publish(f"geng:job:{job_id}", str(event_id))
    except Exception:
        # PostgreSQL is the durable source; Redis only lowers SSE latency.
        return


def serialize_event(event: JobEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "type": event.event_type,
        "phase": event.phase,
        "step": event.step,
        "message": event.message,
        "data": event.data or {},
        "created_at": event.created_at.isoformat(),
    }


def events_after(job_id: str, after_id: int, limit: int = 200) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.scalars(
            select(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.id > after_id)
            .order_by(JobEvent.id)
            .limit(limit)
        ).all()
        return [serialize_event(item) for item in rows]


def touch_heartbeat(job_id: str) -> None:
    append_event(
        job_id,
        {"type": "heartbeat", "message": "worker heartbeat", "data": {"at": datetime.now(timezone.utc).isoformat()}},
    )
