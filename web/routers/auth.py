from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from authlib.integrations.base_client.errors import OAuthError

from web.config import settings
from web.deps import get_auth_service, get_current_user
from domain.auth.schemas import AuthResponse, AuthUserView, LoginRequest, RegisterRequest
from domain.auth.services import AuthService
from infra.oauth.google import get_google_client

router = APIRouter(prefix="/auth", tags=["auth"])
_desktop_handoffs: dict[str, dict[str, object]] = {}
_desktop_tickets: dict[str, str] = {}
_oauth_redirect_targets: dict[str, dict[str, object]] = {}
_DESKTOP_HANDOFF_TTL = timedelta(minutes=10)


def _resolve_redirect_target(candidate: str | None) -> str:
    target = str(candidate or "").strip()
    if not target:
        return settings.frontend_redirect_url

    if target.startswith("/"):
        return target

    parsed = urlparse(target)
    if parsed.scheme == "tauri" and parsed.netloc == "localhost":
        return target

    if parsed.scheme not in {"http", "https"}:
        return settings.frontend_redirect_url

    allowed_netlocs = {
        urlparse(settings.app_base_url).netloc,
        urlparse(settings.frontend_redirect_url).netloc,
        "127.0.0.1:5173",
        "localhost:5173",
        "127.0.0.1:8000",
        "localhost:8000",
    }
    if parsed.netloc in allowed_netlocs:
        return target
    return settings.frontend_redirect_url


def _cleanup_desktop_handoffs(now: datetime | None = None) -> None:
    current = now or datetime.now(UTC)
    expired_flow_ids = [
        flow_id
        for flow_id, payload in _desktop_handoffs.items()
        if not isinstance(payload.get("expires_at"), datetime) or payload["expires_at"] <= current
    ]
    for flow_id in expired_flow_ids:
        payload = _desktop_handoffs.pop(flow_id, None)
        ticket = str(payload.get("ticket", "") or "") if payload else ""
        if ticket:
            _desktop_tickets.pop(ticket, None)

    expired_oauth_states = [
        state
        for state, payload in _oauth_redirect_targets.items()
        if not isinstance(payload.get("expires_at"), datetime) or payload["expires_at"] <= current
    ]
    for state in expired_oauth_states:
        _oauth_redirect_targets.pop(state, None)


def _desktop_flow_id_from_redirect_target(target: str) -> str:
    parsed = urlparse(target)
    if parsed.path != "/auth/desktop/completed":
        return ""
    params = parse_qs(parsed.query)
    return str((params.get("flow_id") or [""])[0] or "")


def _record_desktop_handoff(*, flow_id: str, session_token: str, expires_at, user_view: AuthUserView) -> None:
    if not flow_id:
        return
    _cleanup_desktop_handoffs()
    ticket = secrets.token_urlsafe(24)
    handoff_expires_at = min(expires_at, datetime.now(UTC) + _DESKTOP_HANDOFF_TTL)
    _desktop_handoffs[flow_id] = {
        "ticket": ticket,
        "session_token": session_token,
        "expires_at": handoff_expires_at,
        "user_view": user_view.model_dump(),
    }
    _desktop_tickets[ticket] = flow_id


def _user_view(user) -> AuthUserView:
    return AuthUserView(
        id=user.id,
        email=user.email,
        email_verified=user.email_verified,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )


def _apply_session_cookie(response: JSONResponse | RedirectResponse, token: str, expires_at) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        httponly=True,
        samesite=settings.session_same_site,
        secure=settings.session_secure,
        max_age=settings.session_max_age_seconds,
        expires=expires_at,
        domain=settings.session_domain or None,
        path="/",
    )


@router.post("/register", response_model=AuthResponse)
async def register(
    payload: RegisterRequest,
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
):
    user = await auth_service.register_with_password(
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
    )
    token, expires_at = await auth_service.create_user_session(
        user_id=user.id,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=request.client.host if request.client else "",
    )
    response = JSONResponse({"user": _user_view(user).model_dump(), "session_token": token})
    _apply_session_cookie(response, token, expires_at)
    return response


@router.post("/login", response_model=AuthResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
):
    try:
        user = await auth_service.login_with_password(email=payload.email, password=payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    token, expires_at = await auth_service.create_user_session(
        user_id=user.id,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=request.client.host if request.client else "",
    )
    response = JSONResponse({"user": _user_view(user).model_dump(), "session_token": token})
    _apply_session_cookie(response, token, expires_at)
    return response


@router.get("/google/login")
async def google_login(request: Request):
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=503, detail="Google OAuth 尚未配置")
    _cleanup_desktop_handoffs()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    redirect_to = _resolve_redirect_target(request.query_params.get("redirect_to"))
    _oauth_redirect_targets[state] = {
        "redirect_to": redirect_to,
        "expires_at": datetime.now(UTC) + timedelta(minutes=10),
    }
    client = get_google_client()
    response = await client.authorize_redirect(
        request,
        settings.effective_google_callback_url,
        state=state,
        nonce=nonce,
    )
    response.set_cookie("oauth_state", state, httponly=True, samesite=settings.session_same_site, secure=settings.session_secure, max_age=600, path="/")
    response.set_cookie("oauth_nonce", nonce, httponly=True, samesite=settings.session_same_site, secure=settings.session_secure, max_age=600, path="/")
    response.set_cookie("oauth_redirect_to", redirect_to, httponly=True, samesite=settings.session_same_site, secure=settings.session_secure, max_age=600, path="/")
    return response


@router.get("/google/callback", name="google_callback")
async def google_callback(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
):
    state = request.query_params.get("state", "")
    if request.cookies.get("oauth_state", "") != state:
        raise HTTPException(status_code=400, detail="OAuth state 校验失败")

    client = get_google_client()
    nonce = request.cookies.get("oauth_nonce", "")
    try:
        token = await client.authorize_access_token(request)
        user_info = await client.parse_id_token(token, nonce)
    except OAuthError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Google 登录失败：{exc.error or 'oauth_error'} {exc.description or ''}".strip(),
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Google 登录回调处理失败：{exc}") from exc

    if not user_info:
        raise HTTPException(status_code=400, detail="无法解析 Google 用户信息")

    user = await auth_service.login_with_google(
        email=str(user_info.get("email", "") or ""),
        email_verified=bool(user_info.get("email_verified")),
        provider_user_id=str(user_info.get("sub", "") or ""),
        display_name=str(user_info.get("name", "") or ""),
        avatar_url=str(user_info.get("picture", "") or ""),
    )
    session_token, expires_at = await auth_service.create_user_session(
        user_id=user.id,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=request.client.host if request.client else "",
    )
    redirect_to = _resolve_redirect_target(
        str((_oauth_redirect_targets.pop(state, {}) or {}).get("redirect_to", "") or request.cookies.get("oauth_redirect_to"))
    )
    _record_desktop_handoff(
        flow_id=_desktop_flow_id_from_redirect_target(redirect_to),
        session_token=session_token,
        expires_at=expires_at,
        user_view=_user_view(user),
    )
    response = RedirectResponse(url=redirect_to, status_code=302)
    _apply_session_cookie(response, session_token, expires_at)
    response.delete_cookie("oauth_state", path="/", domain=settings.session_domain or None)
    response.delete_cookie("oauth_nonce", path="/", domain=settings.session_domain or None)
    response.delete_cookie("oauth_redirect_to", path="/", domain=settings.session_domain or None)
    return response


@router.get("/desktop/completed")
async def desktop_auth_completed(request: Request):
    flow_id = escape(str(request.query_params.get("flow_id", "") or ""))
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Chatdada 登录完成</title>
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: #f6f2e9;
        color: #2d2a26;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      main {{
        max-width: 440px;
        background: #fff;
        border-radius: 18px;
        padding: 28px;
        box-shadow: 0 18px 50px rgba(0, 0, 0, 0.12);
      }}
      code {{ background: #f3efe7; padding: 2px 6px; border-radius: 8px; }}
    </style>
  </head>
  <body>
    <main>
      <h1 style="margin-top:0">登录已完成</h1>
      <p>请返回 Chatdada 桌面应用，应用会自动继续完成登录。</p>
      <p style="font-size:12px;color:#6b665f">flow_id: <code>{flow_id or "unknown"}</code></p>
      <script>
        setTimeout(function () {{
          try {{ window.close(); }} catch (_err) {{}}
        }}, 800);
      </script>
    </main>
  </body>
</html>"""
    )


@router.get("/desktop/poll")
async def desktop_auth_poll(flow_id: str):
    _cleanup_desktop_handoffs()
    payload = _desktop_handoffs.get(flow_id)
    if payload is None:
        return {"status": "pending"}
    return {
        "status": "ready",
        "ticket": payload["ticket"],
        "user": payload["user_view"],
    }


@router.post("/desktop/consume", response_model=AuthResponse)
async def desktop_auth_consume(payload: dict):
    _cleanup_desktop_handoffs()
    ticket = str(payload.get("ticket", "") or "")
    flow_id = _desktop_tickets.pop(ticket, "")
    if not flow_id:
        raise HTTPException(status_code=400, detail="Desktop auth ticket 无效或已过期")

    handoff = _desktop_handoffs.pop(flow_id, None)
    if handoff is None:
        raise HTTPException(status_code=400, detail="Desktop auth handoff 不存在或已过期")

    response = JSONResponse({"user": handoff["user_view"], "session_token": handoff["session_token"]})
    _apply_session_cookie(response, str(handoff["session_token"]), handoff["expires_at"])
    return response


@router.post("/logout")
async def logout(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
):
    token = request.cookies.get(settings.session_cookie_name, "")
    if token:
        await auth_service.logout_by_session_token(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(settings.session_cookie_name, path="/", domain=settings.session_domain or None)
    return response


@router.get("/me", response_model=AuthResponse)
async def me(current_user=Depends(get_current_user)):
    return {"user": _user_view(current_user).model_dump()}
