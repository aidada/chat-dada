from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from apps.web.config import settings
from apps.web.middleware import install_session_middleware, register_exception_handlers
from apps.web.routers.auth import router as auth_router
from apps.web.routers.conversations import router as conversation_router
from apps.web.routers.files import router as file_router
from apps.web.routers.quotas import router as quota_router
from apps.web.routers.system import router as system_router
from apps.web.routers.tasks import router as task_router
from apps.web.runtime import FRONTEND_ASSETS_DIR, OUTPUTS_DIR, task_service
from core.langsmith_config import verify_langsmith_connection
from core.logger import setup_logging

setup_logging()
log = logging.getLogger("chatdada.web.app")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await task_service.connect()
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
    await task_service.close()


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

app.include_router(auth_router)
app.include_router(file_router)
app.include_router(quota_router)
app.include_router(task_router)
app.include_router(conversation_router)
app.include_router(system_router)
app.mount("/assets", StaticFiles(directory=str(FRONTEND_ASSETS_DIR), check_dir=False), name="frontend-assets")
app.mount("/download", StaticFiles(directory=str(OUTPUTS_DIR)), name="download-files")
