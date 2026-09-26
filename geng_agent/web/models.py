from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UserRecord(Base):
    __tablename__ = "web_users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SessionRecord(Base):
    __tablename__ = "web_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("web_users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LoginAttempt(Base):
    __tablename__ = "web_login_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    identity: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class CaseRecord(Base):
    __tablename__ = "cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255))
    directory: Mapped[str] = mapped_column(Text, unique=True)
    paper_path: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="upload")
    # NULL owners are preserved historical local cases, never public cases.
    owner_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class JobRecord(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="review")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt: Mapped[int] = mapped_column(default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index(
        "uq_active_job_per_case", "case_id", unique=True,
        postgresql_where=text("status IN ('queued','running','cancel_requested')"),
        sqlite_where=text("status IN ('queued','running','cancel_requested')"),
    ),)


class WorkerAssignment(Base):
    """Durable PC ownership; heartbeat age never changes this assignment."""

    __tablename__ = "worker_assignments"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(128), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (Index(
        "uq_active_assignment_per_worker", "worker_id", unique=True,
        postgresql_where=text("active = true"), sqlite_where=text("active = 1"),
    ),)
