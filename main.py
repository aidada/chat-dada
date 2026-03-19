"""
Local Agent - FastAPI + Multi-Agent + Task Streaming via POST + SSE
运行: source .venv/bin/activate && uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.logger import is_verbose, set_log_level, set_verbose, setup_logging
from runtime.task_runtime import HEARTBEAT_INTERVAL_SECONDS, TaskService, format_sse, task_is_terminal

load_dotenv()

setup_logging()
log = logging.getLogger("chatdada.main")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR = BASE_DIR / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://chatdada:chatdada@localhost:5432/chatdada"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
task_service = TaskService(DATABASE_URL, REDIS_URL)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await task_service.connect()
    yield
    await task_service.close()


app = FastAPI(title="Local Agent", lifespan=lifespan)
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


class TaskCreateRequest(BaseModel):
    task: str
    user_id: str = "anonymous"
    mode: str = "auto"
    thinking_level: str = "medium"
    file_paths: list[str] = Field(default_factory=list)
    conversation_id: str = ""


class TaskReplyRequest(BaseModel):
    answer: str


class VerboseRequest(BaseModel):
    enabled: bool


class LogLevelRequest(BaseModel):
    level: str


class ConversationCreateRequest(BaseModel):
    user_id: str = "anonymous"
    title: str = "新对话"


class ConversationUpdateRequest(BaseModel):
    title: str | None = None
    pinned: bool | None = None


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
    snapshot = await task_service.get_task(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_generator():
        # Subscribe first, then replay — prevents race between publish and replay
        pubsub = await task_service.subscribe(task_id)
        last_sent = after_seq

        try:
            replay_events = await task_service.get_events_after(task_id, after_seq)
            for event in replay_events:
                if int(event["seq"]) > last_sent:
                    last_sent = int(event["seq"])
                    yield format_sse(event)

            while True:
                if await request.is_disconnected():
                    break

                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=HEARTBEAT_INTERVAL_SECONDS,
                )

                if message is None:
                    # Timeout: check task status then send heartbeat or break
                    current = await task_service.get_task(task_id)
                    if current is None or task_is_terminal(current["status"]):
                        break
                    yield ": keep-alive\n\n"
                    continue

                if message["type"] != "message":
                    continue

                try:
                    event = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError):
                    continue

                seq = int(event.get("seq", 0))
                if seq <= last_sent:
                    continue

                last_sent = seq
                yield format_sse(event)

                # After each event, check if task reached terminal status
                current = await task_service.get_task(task_id)
                if current and task_is_terminal(current["status"]):
                    break
        finally:
            await task_service.unsubscribe(task_id, pubsub)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """接收上传文件，保存到 uploads/ 目录，返回服务端路径"""
    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(file.filename).name}"
    dest = UPLOAD_DIR / safe_name
    with open(dest, "wb") as handle:
        shutil.copyfileobj(file.file, handle)
    is_image = Path(file.filename).suffix.lower() in IMAGE_EXTENSIONS
    return {
        "path": str(dest.resolve()),
        "name": file.filename,
        "url": f"/uploads/{safe_name}",
        "is_image": is_image,
    }


@app.get("/")
async def index():
    """返回前端页面"""
    html = Path("static/index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/download/{filename}")
async def download_file(filename: str):
    """下载生成的文件（PPT 等）"""
    safe_name = Path(filename).name
    path = OUTPUTS_DIR / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {safe_name}")
    return FileResponse(path, filename=safe_name)


@app.get("/uploads/{filename}")
async def serve_upload(filename: str):
    """提供上传文件的静态访问（图片展示等）"""
    safe_name = Path(filename).name
    path = UPLOAD_DIR / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path)


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
        conversation_id=payload.conversation_id,
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
    snapshot = await task_service.get_task(task_id)
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

            # Subscribe first, then replay — prevents race between publish and replay
            pubsub = await task_service.subscribe(task_id)
            last_sent = 0

            try:
                replay_events = await task_service.get_events_after(task_id, 0)
                for event in replay_events:
                    if int(event["seq"]) > last_sent:
                        last_sent = int(event["seq"])
                        await websocket.send_json(event)

                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=HEARTBEAT_INTERVAL_SECONDS,
                    )

                    if message is None:
                        current = await task_service.get_task(task_id)
                        if current is None or task_is_terminal(current["status"]):
                            break
                        continue

                    if message["type"] != "message":
                        continue

                    try:
                        event = json.loads(message["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue

                    seq = int(event.get("seq", 0))
                    if seq <= last_sent:
                        continue
                    last_sent = seq
                    await websocket.send_json(event)

                    current = await task_service.get_task(task_id)
                    if current and task_is_terminal(current["status"]):
                        break
            finally:
                await task_service.unsubscribe(task_id, pubsub)
    except WebSocketDisconnect:
        log.info("Client disconnected")


@app.post("/api/verbose")
async def toggle_verbose(req: VerboseRequest):
    set_verbose(req.enabled)
    log.info("Verbose mode set to %s", req.enabled)
    return {"verbose": req.enabled}


# ── Conversations ──


@app.get("/conversations")
async def list_conversations(user_id: str = Query(default="anonymous")):
    items = await task_service.store.list_conversations(user_id)
    return items


@app.post("/conversations")
async def create_conversation(payload: ConversationCreateRequest):
    conv_id = f"conv_{uuid.uuid4().hex[:12]}"
    conv = await task_service.store.create_conversation(
        conversation_id=conv_id,
        user_id=payload.user_id,
        title=payload.title,
    )
    return JSONResponse(conv, status_code=201)


@app.patch("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, payload: ConversationUpdateRequest):
    fields: dict[str, Any] = {}
    if payload.title is not None:
        fields["title"] = payload.title
    if payload.pinned is not None:
        fields["pinned"] = payload.pinned
    if not fields:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    result = await task_service.store.update_conversation(conversation_id, **fields)
    if result is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return result


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    deleted = await task_service.store.delete_conversation(conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="对话不存在")
    return Response(status_code=204)


@app.get("/conversations/{conversation_id}/entries")
async def get_conversation_entries(conversation_id: str):
    entries = await task_service.store.get_conversation_entries(conversation_id)
    return entries


@app.get("/api/verbose")
async def get_verbose():
    return {"verbose": is_verbose()}


@app.post("/api/log-level")
async def change_log_level(req: LogLevelRequest):
    set_log_level(req.level)
    log.info("Log level set to %s", req.level)
    return {"level": req.level}


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/download", StaticFiles(directory=str(OUTPUTS_DIR)), name="download-files")
