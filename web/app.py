from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from web.config import settings
from web.middleware import install_session_middleware, register_exception_handlers
from web.routers.auth import router as auth_router
from web.routers.conversations import router as conversation_router
from web.routers.files import router as file_router
from web.routers.quotas import router as quota_router
from web.routers.system import router as system_router
from web.routers.tasks import router as task_router
from web import runtime as web_runtime
from core.langsmith_config import verify_langsmith_connection
from core.logger import setup_logging

setup_logging()
log = logging.getLogger("chatdada.web.app")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await web_runtime.task_service.connect()
    ls_status = verify_langsmith_connection()
    if ls_status.get("ok"):
        log.info(
            "LangSmith tracing enabled — project=%s endpoint=%s",
            ls_status.get("project"),
            ls_status.get("endpoint"),
        )
    else:
        log.warning("LangSmith tracing unavailable: %s", ls_status.get("reason"))
    yield
    await web_runtime.task_service.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
install_session_middleware(app)
register_exception_handlers(app)

for warning in settings.startup_warnings:
    log.warning("Startup config warning: %s", warning)

log.info(
    "HTTP runtime config: app_base_url=%s cors_allowed_origins=%s session_cookie_name=%s session_secure=%s session_same_site=%s session_domain=%s",
    settings.app_base_url,
    settings.cors_allowed_origins,
    settings.session_cookie_name,
    settings.session_secure,
    settings.session_same_site,
    settings.session_domain or "<none>",
)

app.include_router(auth_router)
app.include_router(file_router)
app.include_router(quota_router)
app.include_router(task_router)
app.include_router(conversation_router)
app.include_router(system_router)

# Desktop Hands WebSocket
from agent.hands.desktop_manager import DesktopHandsManager
from agent.hands.desktop_executor import DesktopToolExecutor
from web.routers.desktop_hands import create_desktop_hands_router

_desktop_manager = DesktopHandsManager()
_desktop_executor = DesktopToolExecutor(_desktop_manager)
web_runtime.task_service.configure_desktop(
    manager=_desktop_manager,
    executor=_desktop_executor,
)

async def _ws_auth(token: str) -> dict | None:
    from infra.db.session import SessionFactory
    from domain.auth.services import AuthService
    async with SessionFactory() as session:
        auth_service = AuthService(session)
        user, _ = await auth_service.get_user_by_session_token(token)
    return {"id": str(user.id), "email": user.email} if user else None

app.include_router(create_desktop_hands_router(
    manager=_desktop_manager,
    executor=_desktop_executor,
    auth_fn=_ws_auth,
))

app.mount("/assets", StaticFiles(directory=str(web_runtime.FRONTEND_ASSETS_DIR), check_dir=False), name="frontend-assets")
app.mount("/download", StaticFiles(directory=str(web_runtime.OUTPUTS_DIR)), name="download-files")
