from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from authlib.integrations.base_client.errors import OAuthError

from web.config import settings
from web.deps import get_auth_service, get_current_user
from domain.auth.schemas import AuthResponse, AuthUserView, LoginRequest, RegisterRequest
from domain.auth.services import AuthService
from infra.oauth.google import get_google_client

router = APIRouter(prefix="/auth", tags=["auth"])


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
    response = JSONResponse({"user": _user_view(user).model_dump()})
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
    response = JSONResponse({"user": _user_view(user).model_dump()})
    _apply_session_cookie(response, token, expires_at)
    return response


@router.get("/google/login")
async def google_login(request: Request):
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=503, detail="Google OAuth 尚未配置")
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    client = get_google_client()
    response = await client.authorize_redirect(
        request,
        settings.effective_google_callback_url,
        state=state,
        nonce=nonce,
    )
    response.set_cookie("oauth_state", state, httponly=True, samesite=settings.session_same_site, secure=settings.session_secure, max_age=600, path="/")
    response.set_cookie("oauth_nonce", nonce, httponly=True, samesite=settings.session_same_site, secure=settings.session_secure, max_age=600, path="/")
    return response


@router.get("/google/callback", name="google_callback")
async def google_callback(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
):
    if request.cookies.get("oauth_state", "") != request.query_params.get("state", ""):
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
    response = RedirectResponse(url=settings.frontend_redirect_url, status_code=302)
    _apply_session_cookie(response, session_token, expires_at)
    response.delete_cookie("oauth_state", path="/", domain=settings.session_domain or None)
    response.delete_cookie("oauth_nonce", path="/", domain=settings.session_domain or None)
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
