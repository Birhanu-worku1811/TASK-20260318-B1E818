from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.infra.db import get_db
from app.infra.response import APIError
from app.infra.security import decode_access_token
from app.models.entities import RoleType, User, UserRoleBinding

bearer_scheme = HTTPBearer(auto_error=False)


def _roles_for_user(db: Session, user_id: uuid.UUID) -> list[str]:
    rows = db.scalars(select(UserRoleBinding).where(UserRoleBinding.user_id == user_id)).all()
    return [r.role.value for r in rows]


def resolve_user_from_token(db: Session, token: str, *, allow_password_change: bool = False) -> User:
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
    except Exception as exc:
        raise APIError(status_code=401, code="invalid_token", message="Invalid access token") from exc
    user = db.get(User, user_id)
    if not user:
        user = db.scalar(select(User).where(User.id == user_id))
    if not user:
        user = db.scalar(select(User).where(User.id == str(user_id)))
    if not user:
        raise APIError(status_code=401, code="invalid_token", message="User no longer exists")
    if not user.is_active:
        raise APIError(status_code=403, code="inactive_user", message="Inactive user cannot access protected resources")
    if not allow_password_change and user.password_change_required:
        raise APIError(
            status_code=403,
            code="password_change_required",
            message="Password change required before accessing protected resources",
        )
    return user


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise APIError(status_code=401, code="missing_token", message="Bearer token is required")
    return resolve_user_from_token(db, credentials.credentials)


def get_current_user_allow_password_change(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise APIError(status_code=401, code="missing_token", message="Bearer token is required")
    return resolve_user_from_token(db, credentials.credentials, allow_password_change=True)


def require_roles(*allowed: RoleType) -> Callable[[User, Session], User]:
    allowed_values = {r.value for r in allowed}

    def _dep(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
        user_roles = set(_roles_for_user(db, current_user.id))
        if not user_roles.intersection(allowed_values):
            raise APIError(status_code=403, code="forbidden", message="Insufficient role permissions")
        return current_user

    return _dep
