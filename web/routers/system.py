from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from web.deps import get_admin_user
from pydantic import BaseModel

from core.langsmith_config import is_langsmith_enabled, set_langsmith_enabled, verify_langsmith_connection
from core.logger import is_verbose, monitor, set_log_level, set_verbose
from agent.brain.defaults import PROVIDERS
from agent.brain.registry import ModelSpec, registry

router = APIRouter(tags=["system"])


class LangSmithToggleRequest(BaseModel):
    enabled: bool


class VerboseRequest(BaseModel):
    enabled: bool


class LogLevelRequest(BaseModel):
    level: str


class ModelUpdateRequest(BaseModel):
    model: str | None = None
    provider: str | None = None


def _serialize_model_config(spec: ModelSpec) -> dict[str, str]:
    return {
        "model": spec.model,
        "provider": spec.provider,
        "client_type": spec.client_type,
    }


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


@router.get("/api/admin/models")
async def list_models(_admin=Depends(get_admin_user)):
    return {
        role: _serialize_model_config(spec)
        for role, spec in registry.snapshot().items()
    }


@router.put("/api/admin/models/{role}")
async def update_model(role: str, req: ModelUpdateRequest, _admin=Depends(get_admin_user)):
    if req.provider is not None and req.provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{req.provider}'")

    try:
        registry.update(role, model=req.model, provider=req.provider)
        spec = registry.get(role)
    except KeyError as exc:
        message = str(exc)
        if "Unknown role" in message:
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=400, detail=message) from exc

    return {"role": role, **_serialize_model_config(spec)}


@router.post("/api/admin/models/reset")
async def reset_models(_admin=Depends(get_admin_user)):
    registry.reset()
    return {"status": "ok", "message": "All models reset to defaults"}
