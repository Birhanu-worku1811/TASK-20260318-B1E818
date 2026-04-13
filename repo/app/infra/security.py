from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from passlib.context import CryptContext

from app.infra.config import get_settings

pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")
settings = get_settings()


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def lock_until(minutes: int) -> datetime:
    return utcnow() + timedelta(minutes=minutes)


def is_locked(locked_until_at: datetime | None) -> bool:
    normalized = normalize_utc(locked_until_at)
    return bool(normalized and normalized > utcnow())


def require_role(user_roles: set[str], required: set[str]) -> bool:
    return bool(user_roles.intersection(required))


def clamp_non_negative(value: Any) -> float:
    return max(0.0, float(value))


def create_access_token(*, subject: str, roles: list[str], expires_minutes: int) -> tuple[str, int]:
    exp = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    payload = {
        "sub": subject,
        "roles": roles,
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_minutes * 60


def decode_access_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
