from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apps.web import runtime as web_runtime
from apps.web.deps import (
    ensure_owner_or_404,
    get_current_user,
    get_task_execution_service,
    resolve_current_user_once,
)
from domain.tasks.services import TaskExecutionService

router = APIRouter(tags=["tasks"])


class TaskCreateRequest(BaseModel):
    task: str
    mode: str = "auto"
    thinking_level: str = "medium"
    file_paths: list[str] = Field(default_factory=list)
    conversation_id: str = ""


class TaskReplyRequest(BaseModel):
    answer: str


@router.post("/tasks")
async def create_task(
    payload: TaskCreateRequest,
    current_user=Depends(get_current_user),
    task_service: TaskExecutionService = Depends(get_task_execution_service),
):
    try:
        task_text, user_id, mode, thinking_level, file_paths = web_runtime.normalize_task_request(
            task=payload.task,
            user_id=current_user.id,
            mode=payload.mode,
            thinking_level=payload.thinking_level,
            file_paths=payload.file_paths,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        snapshot = await task_service.submit(
            task_text=task_text,
            user_id=user_id,
            mode=mode,
            thinking_level=thinking_level,
            file_paths=file_paths,
            conversation_id=payload.conversation_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return JSONResponse({"task_id": snapshot["task_id"], "status": snapshot["status"]}, status_code=202)


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    current_user=Depends(get_current_user),
    task_service: TaskExecutionService = Depends(get_task_execution_service),
):
    snapshot = await task_service.get(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_owner_or_404(resource_user_id=str(snapshot.get("user_id", "")), current_user=current_user)
    return snapshot


@router.get("/tasks/{task_id}/artifacts")
async def get_task_artifacts(
    task_id: str,
    current_user=Depends(get_current_user),
    task_service: TaskExecutionService = Depends(get_task_execution_service),
):
    snapshot = await task_service.get(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_owner_or_404(resource_user_id=str(snapshot.get("user_id", "")), current_user=current_user)
    return {"task_id": task_id, "artifact_refs": snapshot.get("artifact_refs", [])}


@router.get("/tasks/{task_id}/review")
async def get_task_review(
    task_id: str,
    current_user=Depends(get_current_user),
    task_service: TaskExecutionService = Depends(get_task_execution_service),
):
    snapshot = await task_service.get(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_owner_or_404(resource_user_id=str(snapshot.get("user_id", "")), current_user=current_user)
    return {"task_id": task_id, "review": snapshot.get("review"), "budget": snapshot.get("budget")}


@router.get("/tasks/{task_id}/provenance")
async def get_task_provenance(
    task_id: str,
    current_user=Depends(get_current_user),
    task_service: TaskExecutionService = Depends(get_task_execution_service),
):
    snapshot = await task_service.get(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_owner_or_404(resource_user_id=str(snapshot.get("user_id", "")), current_user=current_user)
    events = await task_service.get_events_after(task_id, 0)
    supporting_events = [
        event
        for event in events
        if event["type"] in {"file", "result", "question", "user_reply", "error"}
    ]
    return {
        "task_id": task_id,
        "domain": snapshot.get("domain"),
        "artifact_refs": snapshot.get("artifact_refs", []),
        "events": supporting_events,
    }


@router.get("/tasks/{task_id}/trace")
async def get_task_trace(
    task_id: str,
    current_user=Depends(get_current_user),
    task_service: TaskExecutionService = Depends(get_task_execution_service),
):
    snapshot = await task_service.get(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_owner_or_404(resource_user_id=str(snapshot.get("user_id", "")), current_user=current_user)
    events = await task_service.get_events_after(task_id, 0)
    monitoring = [event for event in events if event["type"] == "monitoring"]
    if not monitoring:
        return {"task_id": task_id, "trace": None}
    return {"task_id": task_id, "trace": monitoring[-1]["content"]}


@router.get("/tasks/{task_id}/replay")
async def get_task_replay(
    task_id: str,
    current_user=Depends(get_current_user),
    task_service: TaskExecutionService = Depends(get_task_execution_service),
):
    snapshot = await task_service.get(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_owner_or_404(resource_user_id=str(snapshot.get("user_id", "")), current_user=current_user)
    events = await task_service.get_events_after(task_id, 0)
    return {"task": snapshot, "events": events}


@router.post("/tasks/{task_id}/reply")
async def reply_task(
    task_id: str,
    payload: TaskReplyRequest,
    current_user=Depends(get_current_user),
    task_service: TaskExecutionService = Depends(get_task_execution_service),
):
    answer = str(payload.answer or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="回复内容不能为空")
    snapshot = await task_service.get(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_owner_or_404(resource_user_id=str(snapshot.get("user_id", "")), current_user=current_user)
    try:
        snapshot = await task_service.reply(task_id, answer)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"task_id": snapshot["task_id"], "status": snapshot["status"]}, status_code=202)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    current_user=Depends(get_current_user),
    task_service: TaskExecutionService = Depends(get_task_execution_service),
):
    snapshot = await task_service.get(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_owner_or_404(resource_user_id=str(snapshot.get("user_id", "")), current_user=current_user)
    try:
        snapshot = await task_service.cancel(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"task_id": snapshot["task_id"], "status": snapshot["status"]}, status_code=202)


@router.get("/tasks/{task_id}/events")
async def stream_task_events(
    request: Request,
    task_id: str,
    after_seq: int | None = Query(default=None, ge=0),
    task_service: TaskExecutionService = Depends(get_task_execution_service),
):
    current_user = await resolve_current_user_once(request)
    snapshot = await task_service.get(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_owner_or_404(resource_user_id=str(snapshot.get("user_id", "")), current_user=current_user)
    return await web_runtime.event_stream_response(
        request,
        task_id,
        web_runtime.parse_after_seq(request, after_seq),
        snapshot=snapshot,
    )
