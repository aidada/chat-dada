from __future__ import annotations

import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from agent.session.runtime import SessionRuntime
from agent.runtime.task_execution import (
    HEARTBEAT_INTERVAL_SECONDS,
    TaskService,
    format_sse,
    task_is_terminal,
)
from domain.conversations.context import ConversationContextBuilder
from domain.conversations.services import ConversationEmbeddingService, ConversationService
from infra.db.session_store import PostgresSessionStore
from web.config import settings
from infra.db.session import engine as app_db_engine

log = logging.getLogger("chatdada.web")

BASE_DIR = Path(__file__).resolve().parents[2]
WORKSPACE_DIR = BASE_DIR.parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR = BASE_DIR / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)

FRONTEND_DIST_DIR = Path(
    settings.frontend_dist_dir or (WORKSPACE_DIR / "chat-dada-front" / "dist")
).resolve()
FRONTEND_ASSETS_DIR = FRONTEND_DIST_DIR / "assets"


def _build_checkpointer_factory(database_url: str):
    def factory():
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError:
            from langgraph.checkpoint.memory import InMemorySaver

            log.warning("langgraph-checkpoint-postgres not installed; falling back to InMemorySaver")
            return InMemorySaver()
        return AsyncPostgresSaver.from_conn_string(database_url)

    return factory


def build_task_service(database_url: str, redis_url: str) -> TaskService:
    redis = aioredis.from_url(redis_url, decode_responses=True)
    session = SessionRuntime(redis, PostgresSessionStore())
    return TaskService(
        session=session,
        redis=redis,
        checkpointer_factory=_build_checkpointer_factory(database_url),
        conversation_context_builder_factory=ConversationContextBuilder,
        embedding_service=ConversationEmbeddingService(),
        conversation_service=ConversationService(),
    )


task_service = build_task_service(settings.database_url, settings.redis_url)
_active_sse_connections = 0
_task_sse_connections: dict[str, int] = {}


def normalize_task_request(
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


def parse_after_seq(request: Request, after_seq: int | None) -> int:
    if after_seq is not None:
        return max(after_seq, 0)
    header_value = request.headers.get("last-event-id", "").strip()
    if not header_value:
        return 0
    try:
        return max(int(header_value), 0)
    except ValueError:
        return 0


async def index_response() -> HTMLResponse:
    html_path = FRONTEND_DIST_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse(
            (
                "<h1>前端尚未构建</h1>"
                "<p>请先在 <code>chat-dada-front/</code> 目录执行 <code>npm install</code> 和 "
                "<code>npm run build</code>。</p>"
            ),
            status_code=503,
        )
    html = html_path.read_text(encoding="utf-8")
    return HTMLResponse(html)


async def event_stream_response(
    request: Request,
    task_id: str,
    after_seq: int,
    snapshot: dict[str, Any] | None = None,
    stream_metadata: dict[str, Any] | None = None,
) -> StreamingResponse:
    snapshot = snapshot or await task_service.get_task(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    def _db_pool_in_use() -> int | None:
        pool = getattr(app_db_engine.sync_engine, "pool", None)
        if pool is None:
            return None
        checked_out = getattr(pool, "checkedout", None)
        if callable(checked_out):
            try:
                return int(checked_out())
            except Exception:
                return None
        return None

    def _stream_meta() -> dict[str, Any]:
        redis_pool = getattr(task_service.redis, "connection_pool", None)
        redis_in_use = None
        if redis_pool is not None:
            created = getattr(redis_pool, "_created_connections", None)
            available = getattr(redis_pool, "_available_connections", None)
            if isinstance(created, int) and isinstance(available, list):
                try:
                    redis_in_use = int(created - len(available))
                except Exception:
                    redis_in_use = None
        return {
            "active_sse_connections": _active_sse_connections,
            "task_sse_connections": int(_task_sse_connections.get(task_id, 0)),
            "db_pool_in_use": _db_pool_in_use(),
            "task_store_pool_in_use": redis_in_use,
            **dict(stream_metadata or {}),
        }

    def _event_with_meta(event: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(event)
        enriched["stream_meta"] = _stream_meta()
        return enriched

    async def event_generator():
        global _active_sse_connections
        _active_sse_connections += 1
        _task_sse_connections[task_id] = int(_task_sse_connections.get(task_id, 0)) + 1
        pubsub = await task_service.subscribe(task_id)
        last_sent = after_seq
        try:
            replay_events = await task_service.get_events_after(task_id, after_seq)
            for event in replay_events:
                if int(event["seq"]) > last_sent:
                    last_sent = int(event["seq"])
                    yield format_sse(_event_with_meta(event))

            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=HEARTBEAT_INTERVAL_SECONDS,
                )
                if message is None:
                    current = await task_service.get_task(task_id)
                    if current is None or task_is_terminal(current["status"]):
                        break
                    yield f": keep-alive {json.dumps(_stream_meta(), ensure_ascii=False)}\n\n"
                    continue
                if message["type"] != "message":
                    continue
                try:
                    event = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if event.get("stream_kind", "canonical") == "canonical":
                    seq = int(event.get("seq", 0))
                    if seq <= last_sent:
                        continue
                    last_sent = seq
                yield format_sse(_event_with_meta(event))
                current = await task_service.get_task(task_id)
                if current and task_is_terminal(current["status"]):
                    break
        finally:
            _active_sse_connections = max(_active_sse_connections - 1, 0)
            _task_sse_connections[task_id] = max(int(_task_sse_connections.get(task_id, 1)) - 1, 0)
            if _task_sse_connections[task_id] == 0:
                _task_sse_connections.pop(task_id, None)
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


def save_upload(upload_file) -> dict[str, Any]:
    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(upload_file.filename).name}"
    dest = UPLOAD_DIR / safe_name
    with open(dest, "wb") as handle:
        shutil.copyfileobj(upload_file.file, handle)
    is_image = Path(upload_file.filename).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    return {
        "path": str(dest.resolve()),
        "name": upload_file.filename,
        "url": f"/uploads/{safe_name}",
        "is_image": is_image,
    }
