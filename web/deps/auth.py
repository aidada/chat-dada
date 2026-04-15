from __future__ import annotations

import time

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from web.config import settings
from domain.auth.services import AuthService
from infra.db.session import SessionFactory, get_db_session


def _extract_session_token(request: Request) -> str:
    token = request.cookies.get(settings.session_cookie_name, "")
    if token:
        return token
    header_token = request.headers.get("x-session-token", "")
    if header_token:
        return header_token
    query_token = request.query_params.get("session_token", "")
    if query_token:
        return query_token
    return ""


async def get_auth_service(session: AsyncSession = Depends(get_db_session)) -> AuthService:
    return AuthService(session)


async def get_current_user(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
):
    token = _extract_session_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    user, _session = await auth_service.get_user_by_session_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="登录状态已失效")
    return user


async def get_optional_current_user(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
):
    token = _extract_session_token(request)
    if not token:
        return None
    user, _session = await auth_service.get_user_by_session_token(token)
    return user


async def resolve_current_user_once(request: Request):
    """Resolve the current user with a short-lived DB session.

    This is intended for long-lived streaming routes such as SSE, where a
    request-scoped dependency session would otherwise stay open until the stream
    closes and tie up a pooled connection for the entire subscription.
    """

    user, _meta = await resolve_current_user_once_with_metadata(request)
    return user


async def resolve_current_user_once_with_metadata(request: Request):
    token = _extract_session_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")

    started_at = time.perf_counter()
    async with SessionFactory() as session:
        auth_service = AuthService(session)
        user, _session = await auth_service.get_user_by_session_token(token)
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)

    if user is None:
        raise HTTPException(status_code=401, detail="登录状态已失效")
    return user, {"auth_lookup_ms": duration_ms}


def resolve_request_user_id(current_user, requested_user_id: str | None = None) -> str:
    if current_user is not None:
        return current_user.id
    fallback = str(requested_user_id or "").strip()
    return fallback or "anonymous"


def ensure_owner_or_404(*, resource_user_id: str, current_user) -> None:
    if current_user is not None:
        if resource_user_id != current_user.id:
            raise HTTPException(status_code=404, detail="资源不存在")
        return
    if resource_user_id != "anonymous":
        raise HTTPException(status_code=401, detail="需要登录")
