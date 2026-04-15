from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from web.config import settings


def install_session_middleware(app: FastAPI) -> None:
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_session_secret,
        same_site=settings.session_same_site,
        https_only=settings.session_secure,
        session_cookie="chat_dada_oauth_state",
    )
