from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from core.langsmith_config import is_langsmith_enabled, set_langsmith_enabled, verify_langsmith_connection
from core.logger import is_verbose, monitor, set_log_level, set_verbose

router = APIRouter(tags=["system"])


class LangSmithToggleRequest(BaseModel):
    enabled: bool


class VerboseRequest(BaseModel):
    enabled: bool


class LogLevelRequest(BaseModel):
    level: str


@router.get("/api/traces")
async def list_trace_history():
    return {"items": monitor.get_history()}


@router.get("/api/langsmith")
async def get_langsmith_status():
    status = verify_langsmith_connection()
    return {"enabled": is_langsmith_enabled(), **status}


@router.post("/api/langsmith")
async def toggle_langsmith(req: LangSmithToggleRequest):
    set_langsmith_enabled(req.enabled)
    return {"enabled": is_langsmith_enabled()}


@router.post("/api/verbose")
async def toggle_verbose(req: VerboseRequest):
    set_verbose(req.enabled)
    return {"verbose": req.enabled}


@router.get("/api/verbose")
async def get_verbose():
    return {"verbose": is_verbose()}


@router.post("/api/log-level")
async def change_log_level(req: LogLevelRequest):
    set_log_level(req.level)
    return {"level": req.level}
