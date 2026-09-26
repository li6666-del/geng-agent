"""Email/password accounts and revocable, cookie-based sessions."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import get_session
from .models import LoginAttempt, SessionRecord, UserRecord
from .settings import settings

router = APIRouter(prefix="/api/v1/auth")
COOKIE = "geng_session"


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=10, max_length=128)


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


_DUMMY_HASH = password_hash("unused-account-password")


def password_matches(password: str, stored: str) -> bool:
    try:
        _, salt, _ = stored.split("$")
        return hmac.compare_digest(password_hash(password, salt), stored)
    except (ValueError, TypeError):
        return False


def same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if (parsed.scheme, parsed.netloc) != (request.url.scheme, request.url.netloc):
            raise HTTPException(403, "请求来源不匹配，请从网站页面重试")


def current_session(request: Request, session: Session = Depends(get_session)) -> SessionRecord:
    token = request.cookies.get(COOKIE, "")
    record = session.get(SessionRecord, hashlib.sha256(token.encode()).hexdigest()) if token else None
    if record is None or record.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(401, "请先登录")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        same_origin(request)
        if not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), record.csrf_token.encode()):
            raise HTTPException(403, "登录状态需要刷新，请重新打开页面")
    return record


def current_user(record: SessionRecord = Depends(current_session), session: Session = Depends(get_session)) -> UserRecord:
    user = session.get(UserRecord, record.user_id)
    if user is None:
        raise HTTPException(401, "请重新登录")
    return user


def _session_body(user: UserRecord, record: SessionRecord) -> dict:
    return {"user": {"id": user.id, "email": user.email}, "csrf_token": record.csrf_token}


def _sign_in(user: UserRecord, request: Request, response: Response, session: Session) -> dict:
    previous = request.cookies.get(COOKIE)
    if previous:
        session.execute(delete(SessionRecord).where(SessionRecord.token_hash == hashlib.sha256(previous.encode()).hexdigest()))
    now = datetime.now(timezone.utc)
    session.execute(delete(SessionRecord).where(SessionRecord.expires_at < now))
    token = secrets.token_urlsafe(32)
    record = SessionRecord(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                           csrf_token=secrets.token_hex(32), expires_at=now + timedelta(days=settings.session_days))
    session.add(record)
    session.commit()
    response.set_cookie(COOKIE, token, max_age=settings.session_days * 86400,
                        httponly=True, secure=settings.cookie_secure, samesite="lax", path="/")
    return _session_body(user, record)


def _credentials_email(credentials: Credentials) -> str:
    email = credentials.email.strip().casefold()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(422, "请输入有效的邮箱地址")
    return email


def _record_attempt(request: Request, email: str, session: Session) -> None:
    """Persist auth throttling so it also applies across Web worker processes."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
    session.execute(delete(LoginAttempt).where(LoginAttempt.created_at < cutoff))
    identities = [hashlib.sha256(f"email:{email}".encode()).hexdigest(),
                  hashlib.sha256(f"ip:{request.client.host if request.client else 'unknown'}".encode()).hexdigest()]
    for identity, limit in zip(identities, (15, 100)):
        count = session.scalar(select(func.count()).select_from(LoginAttempt).where(LoginAttempt.identity == identity)) or 0
        if count >= limit:
            raise HTTPException(429, "尝试次数过多，请 15 分钟后再试")
        session.add(LoginAttempt(id=str(uuid.uuid4()), identity=identity))
    session.commit()


@router.post("/register", status_code=201)
def register(credentials: Credentials, request: Request, response: Response, session: Session = Depends(get_session)) -> dict:
    same_origin(request)
    if not settings.registration_enabled:
        raise HTTPException(403, "暂未开放新账号注册")
    email = _credentials_email(credentials)
    _record_attempt(request, email, session)
    user = UserRecord(id=str(uuid.uuid4()), email=email, password_hash=password_hash(credentials.password))
    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "该邮箱已注册，请登录") from exc
    return _sign_in(user, request, response, session)


@router.post("/login")
def login(credentials: Credentials, request: Request, response: Response, session: Session = Depends(get_session)) -> dict:
    same_origin(request)
    email = _credentials_email(credentials)
    _record_attempt(request, email, session)
    user = session.scalar(select(UserRecord).where(UserRecord.email == email))
    valid = password_matches(credentials.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not valid:
        raise HTTPException(401, "邮箱或密码不正确")
    return _sign_in(user, request, response, session)


@router.get("/session")
def session_info(record: SessionRecord = Depends(current_session), session: Session = Depends(get_session)) -> dict:
    user = session.get(UserRecord, record.user_id)
    if user is None:
        raise HTTPException(401, "请重新登录")
    return _session_body(user, record)


@router.post("/logout", status_code=204)
def logout(response: Response, record: SessionRecord = Depends(current_session), session: Session = Depends(get_session)) -> None:
    session.delete(record)
    session.commit()
    response.delete_cookie(COOKIE, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
