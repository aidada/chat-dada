"""
Local Agent - FastAPI + Multi-Agent + Task Streaming via POST + SSE
运行: source .venv/bin/activate && uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from logger import is_verbose, set_log_level, set_verbose, setup_logging
from task_runtime import HEARTBEAT_INTERVAL_SECONDS, TaskService, format_sse, task_is_terminal

load_dotenv()

setup_logging()
log = logging.getLogger("chatdada.main")

app = FastAPI(title="Local Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务端内部错误，请查看后端日志。"},
    )

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

TASK_DB_PATH = Path("data/task_runs.sqlite3")
task_service = TaskService(TASK_DB_PATH)


class TaskCreateRequest(BaseModel):
    task: str
    user_id: str = "anonymous"
    mode: str = "auto"
    thinking_level: str = "medium"
    file_paths: list[str] = Field(default_factory=list)


class TaskReplyRequest(BaseModel):
    answer: str


class VerboseRequest(BaseModel):
    enabled: bool


class LogLevelRequest(BaseModel):
    level: str


def _normalize_task_request(
    *,
    task: Any,
    user_id: Any,
    mode: Any,
    thinking_level: Any,
    file_paths: Any,
) -> tuple[str, str, str, str, list[str]]:
    task_text = str(task or "").strip()
    user_value = str(user_id or "anonymous").strip() or "anonymous"
    mode_value = str(mode or "auto").strip().lower()
    if mode_value not in {"auto", "chat", "agent"}:
        mode_value = "auto"
    level_value = str(thinking_level or "medium").strip().lower()
    if level_value not in {"low", "medium", "high"}:
        level_value = "medium"

    normalized_paths: list[str] = []
    if isinstance(file_paths, list):
        for item in file_paths:
            path = str(item or "").strip()
            if path:
                normalized_paths.append(path)

    if not task_text:
        raise ValueError("任务不能为空")

    if mode_value == "chat" and normalized_paths:
        raise ValueError("chat 模式暂不支持附件，请改用 auto 或 agent 模式")

    return task_text, user_value, mode_value, level_value, normalized_paths


def _parse_after_seq(request: Request, after_seq: int | None) -> int:
    if after_seq is not None:
        return max(after_seq, 0)

    header_value = request.headers.get("last-event-id", "").strip()
    if not header_value:
        return 0

    try:
        return max(int(header_value), 0)
    except ValueError:
        return 0


async def _event_stream(request: Request, task_id: str, after_seq: int) -> StreamingResponse:
    snapshot = task_service.get_task(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_generator():
        queue = task_service.subscribe(task_id)
        last_sent = after_seq

        try:
            replay_events = task_service.get_events_after(task_id, after_seq)
            for event in replay_events:
                last_sent = max(last_sent, int(event["seq"]))
                yield format_sse(event)

            while True:
                if await request.is_disconnected():
                    break

                current = task_service.get_task(task_id)
                if current is None:
                    break
                if task_is_terminal(current["status"]) and queue.empty():
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue

                if int(event["seq"]) <= last_sent:
                    continue

                last_sent = int(event["seq"])
                yield format_sse(event)
        finally:
            task_service.unsubscribe(task_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """接收上传文件，保存到 uploads/ 目录，返回服务端路径"""
    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(file.filename).name}"
    dest = UPLOAD_DIR / safe_name
    with open(dest, "wb") as handle:
        shutil.copyfileobj(file.file, handle)
    return {"path": str(dest.resolve()), "name": file.filename}


@app.get("/")
async def index():
    """返回前端页面"""
    html = Path("static/index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/download/{filename}")
async def download_file(filename: str):
    """下载生成的文件（PPT 等）"""
    safe_name = Path(filename).name
    path = Path("outputs") / safe_name
    if not path.exists():
        return {"error": "文件不存在"}
    return FileResponse(path, filename=safe_name)


@app.post("/tasks")
async def create_task(payload: TaskCreateRequest):
    try:
        task_text, user_id, mode, thinking_level, file_paths = _normalize_task_request(
            task=payload.task,
            user_id=payload.user_id,
            mode=payload.mode,
            thinking_level=payload.thinking_level,
            file_paths=payload.file_paths,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    snapshot = await task_service.submit_task(
        task_text=task_text,
        user_id=user_id,
        mode=mode,
        thinking_level=thinking_level,
        file_paths=file_paths,
    )
    return JSONResponse(
        {
            "task_id": snapshot["task_id"],
            "status": snapshot["status"],
        },
        status_code=202,
    )


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    snapshot = task_service.get_task(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return snapshot


@app.post("/tasks/{task_id}/reply")
async def reply_task(task_id: str, payload: TaskReplyRequest):
    answer = str(payload.answer or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="回复内容不能为空")

    try:
        snapshot = await task_service.reply_to_task(task_id, answer)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return JSONResponse(
        {
            "task_id": snapshot["task_id"],
            "status": snapshot["status"],
        },
        status_code=202,
    )


@app.get("/tasks/{task_id}/events")
async def stream_task_events(
    request: Request,
    task_id: str,
    after_seq: int | None = Query(default=None, ge=0),
):
    return await _event_stream(request, task_id, _parse_after_seq(request, after_seq))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Deprecated compatibility path.
    New clients should use POST /tasks + GET /tasks/{task_id}/events instead.
    """
    await websocket.accept()
    log.info("Client connected")

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            try:
                task_text, user_id, mode, thinking_level, file_paths = _normalize_task_request(
                    task=data.get("task"),
                    user_id=data.get("user_id"),
                    mode=data.get("mode"),
                    thinking_level=data.get("thinking_level"),
                    file_paths=data.get("file_paths"),
                )
            except ValueError as exc:
                await websocket.send_json({"type": "error", "content": str(exc)})
                continue

            snapshot = await task_service.submit_task(
                task_text=task_text,
                user_id=user_id,
                mode=mode,
                thinking_level=thinking_level,
                file_paths=file_paths,
            )
            task_id = snapshot["task_id"]
            queue = task_service.subscribe(task_id)
            last_sent = 0

            try:
                replay_events = task_service.get_events_after(task_id, 0)
                for event in replay_events:
                    last_sent = max(last_sent, int(event["seq"]))
                    await websocket.send_json(event)

                while True:
                    current = task_service.get_task(task_id)
                    if current is None:
                        break
                    if task_is_terminal(current["status"]) and queue.empty():
                        break

                    event = await queue.get()
                    if int(event["seq"]) <= last_sent:
                        continue
                    last_sent = int(event["seq"])
                    await websocket.send_json(event)
            finally:
                task_service.unsubscribe(task_id, queue)
    except WebSocketDisconnect:
        log.info("Client disconnected")


@app.post("/api/verbose")
async def toggle_verbose(req: VerboseRequest):
    set_verbose(req.enabled)
    log.info("Verbose mode set to %s", req.enabled)
    return {"verbose": req.enabled}


@app.get("/api/verbose")
async def get_verbose():
    return {"verbose": is_verbose()}


@app.post("/api/log-level")
async def change_log_level(req: LogLevelRequest):
    set_log_level(req.level)
    log.info("Log level set to %s", req.level)
    return {"level": req.level}


app.mount("/static", StaticFiles(directory="static"), name="static")
