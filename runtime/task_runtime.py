from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Callable as AbcCallable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable

import asyncpg
import redis.asyncio as aioredis

from core.logger import monitor, new_trace_id
from core.models import set_thinking_level
from runtime.task_dispatcher import RouteDecision, dispatch_task
from runtime.task_interaction import reset_task_interaction_handler, set_task_interaction_handler

log = logging.getLogger("chatdada.tasks")

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
HEARTBEAT_INTERVAL_SECONDS = 10

TaskDispatcher = AbcCallable[[str, list[str], str, str], Awaitable[RouteDecision]]


def compose_task_text(task: str, file_paths: list[str]) -> str:
    if not file_paths:
        return task
    file_list = "\n".join(f"  - {path}" for path in file_paths)
    return f"{task}\n\n[用户上传了以下文件，请在任务中使用这些文件]:\n{file_list}"


def task_is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def parse_step_payload(step_info: str) -> tuple[str, dict[str, Any]]:
    try:
        parsed = json.loads(step_info)
    except (json.JSONDecodeError, TypeError):
        return "step", {"content": str(step_info)}

    if isinstance(parsed, dict) and isinstance(parsed.get("type"), str):
        payload = dict(parsed)
        event_type = str(payload.pop("type"))
        if event_type == "file":
            payload.setdefault("content", payload.get("name") or payload.get("url") or "")
        else:
            payload.setdefault("content", str(payload.get("content", "")))
        return event_type, payload

    return "step", {"content": str(step_info)}


class TaskRunStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self._database_url, min_size=2, max_size=10)
        await self._recover_interrupted_tasks()

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def _recover_interrupted_tasks(self) -> None:
        rows = await self.pool.fetch(
            """
            SELECT task_id
            FROM task_runs
            WHERE status IN ('queued', 'running', 'waiting_for_user')
            ORDER BY created_at ASC
            """
        )
        if not rows:
            return

        log.warning("Recovering %s interrupted task(s) after process restart", len(rows))
        for row in rows:
            task_id = row["task_id"]
            snapshot = await self.get_task(task_id) or {}
            if snapshot.get("status") == "waiting_for_user":
                message = "任务在等待用户补充时因服务重启而中断，请重新提交。"
            else:
                message = "任务因服务重启而中断，请重新提交。"
            await self.set_error_text(task_id, message)
            await self.append_event(
                task_id,
                "error",
                {"content": message, "recovered": True},
            )
            await self.append_event(
                task_id,
                "monitoring",
                {
                    "content": {
                        "trace_id": None,
                        "total_duration_ms": 0,
                        "llm_call_count": 0,
                        "total_tokens": 0,
                        "error_count": 1,
                        "events": [],
                        "interrupted": True,
                    }
                },
            )
            await self.finish_task(task_id, "failed")

    async def create_task(
        self,
        *,
        user_id: str,
        task_text: str,
        mode: str,
        thinking_level: str,
        request_payload: dict[str, Any],
        conversation_id: str = "",
    ) -> dict[str, Any]:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)

        await self.pool.execute(
            """
            INSERT INTO task_runs (
                task_id, user_id, status, task_text, mode, thinking_level,
                request_payload, conversation_id, created_at, updated_at
            ) VALUES ($1, $2, 'queued', $3, $4, $5, $6::jsonb, $7, $8, $9)
            """,
            task_id,
            user_id,
            task_text,
            mode,
            thinking_level,
            json.dumps(request_payload, ensure_ascii=False),
            conversation_id or None,
            now,
            now,
        )

        if conversation_id:
            await self.pool.execute(
                "UPDATE conversations SET updated_at = $1 WHERE id = $2",
                now,
                conversation_id,
            )

        return await self.get_task(task_id) or {
            "task_id": task_id,
            "status": "queued",
            "task": task_text,
            "mode": mode,
            "thinking_level": thinking_level,
        }

    async def mark_started(self, task_id: str) -> None:
        now = datetime.now(UTC)
        await self.pool.execute(
            """
            UPDATE task_runs
            SET status = 'running',
                started_at = COALESCE(started_at, $1),
                pending_question = NULL,
                updated_at = $2
            WHERE task_id = $3
            """,
            now,
            now,
            task_id,
        )

    async def set_waiting_for_user(self, task_id: str, question_payload: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        await self.pool.execute(
            """
            UPDATE task_runs
            SET status = 'waiting_for_user',
                pending_question = $1::jsonb,
                updated_at = $2
            WHERE task_id = $3
            """,
            json.dumps(question_payload, ensure_ascii=False),
            now,
            task_id,
        )

    async def resume_task(self, task_id: str) -> None:
        now = datetime.now(UTC)
        await self.pool.execute(
            """
            UPDATE task_runs
            SET status = 'running',
                pending_question = NULL,
                updated_at = $1
            WHERE task_id = $2
            """,
            now,
            task_id,
        )

    async def set_route_info(
        self,
        task_id: str,
        *,
        route_name: str,
        route_reason: str,
        route_confidence: float,
    ) -> None:
        now = datetime.now(UTC)
        await self.pool.execute(
            """
            UPDATE task_runs
            SET route_name = $1,
                route_reason = $2,
                route_confidence = $3,
                updated_at = $4
            WHERE task_id = $5
            """,
            route_name,
            route_reason,
            route_confidence,
            now,
            task_id,
        )

    async def set_result_text(self, task_id: str, result_text: str) -> None:
        now = datetime.now(UTC)
        await self.pool.execute(
            """
            UPDATE task_runs
            SET result_text = $1, error_text = NULL, updated_at = $2
            WHERE task_id = $3
            """,
            result_text,
            now,
            task_id,
        )

    async def set_error_text(self, task_id: str, error_text: str) -> None:
        now = datetime.now(UTC)
        await self.pool.execute(
            """
            UPDATE task_runs
            SET error_text = $1, updated_at = $2
            WHERE task_id = $3
            """,
            error_text,
            now,
            task_id,
        )

    async def finish_task(self, task_id: str, status: str) -> None:
        now = datetime.now(UTC)
        await self.pool.execute(
            """
            UPDATE task_runs
            SET status = $1, finished_at = $2, pending_question = NULL, updated_at = $3
            WHERE task_id = $4
            """,
            status,
            now,
            now,
            task_id,
        )

    async def append_event(
        self, task_id: str, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        created_at = datetime.now(UTC)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM task_events WHERE task_id = $1",
                    task_id,
                )
                seq = int(row["seq"])
                await conn.execute(
                    """
                    INSERT INTO task_events (task_id, seq, event_type, payload, created_at)
                    VALUES ($1, $2, $3, $4::jsonb, $5)
                    """,
                    task_id,
                    seq,
                    event_type,
                    json.dumps(payload, ensure_ascii=False),
                    created_at,
                )
                await conn.execute(
                    "UPDATE task_runs SET updated_at = $1 WHERE task_id = $2",
                    created_at,
                    task_id,
                )

        event: dict[str, Any] = {
            "task_id": task_id,
            "seq": seq,
            "type": event_type,
            "created_at": created_at.isoformat(),
        }
        event.update(payload)
        return event

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    task_id,
                    user_id,
                    status,
                    task_text,
                    mode,
                    thinking_level,
                    route_name,
                    route_reason,
                    route_confidence,
                    request_payload,
                    pending_question,
                    result_text,
                    error_text,
                    conversation_id,
                    created_at,
                    started_at,
                    finished_at,
                    updated_at
                FROM task_runs
                WHERE task_id = $1
                """,
                task_id,
            )
            if row is None:
                return None

            last_seq_row = await conn.fetchrow(
                "SELECT COALESCE(MAX(seq), 0) AS last_seq FROM task_events WHERE task_id = $1",
                task_id,
            )

        payload = json.loads(row["request_payload"])
        pending_question = None
        if row["pending_question"]:
            try:
                pending_question = json.loads(row["pending_question"])
            except (json.JSONDecodeError, TypeError):
                pending_question = {"content": str(row["pending_question"])}

        def _ts(v: datetime | None) -> str | None:
            return v.isoformat() if v is not None else None

        return {
            "task_id": row["task_id"],
            "user_id": row["user_id"],
            "status": row["status"],
            "task": row["task_text"],
            "mode": row["mode"],
            "thinking_level": row["thinking_level"],
            "route_name": row["route_name"],
            "route_reason": row["route_reason"],
            "route_confidence": row["route_confidence"],
            "file_paths": payload.get("file_paths", []),
            "pending_question": pending_question,
            "result": row["result_text"],
            "error": row["error_text"],
            "created_at": _ts(row["created_at"]),
            "started_at": _ts(row["started_at"]),
            "finished_at": _ts(row["finished_at"]),
            "updated_at": _ts(row["updated_at"]),
            "conversation_id": row["conversation_id"] or "",
            "last_seq": int(last_seq_row["last_seq"]) if last_seq_row else 0,
        }

    async def get_events_after(self, task_id: str, after_seq: int) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT task_id, seq, event_type, payload, created_at
            FROM task_events
            WHERE task_id = $1 AND seq > $2
            ORDER BY seq ASC
            """,
            task_id,
            after_seq,
        )
        events: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload"])
            event: dict[str, Any] = {
                "task_id": row["task_id"],
                "seq": int(row["seq"]),
                "type": row["event_type"],
                "created_at": row["created_at"].isoformat(),
            }
            event.update(payload)
            events.append(event)
        return events

    # ── Conversation CRUD ──

    async def create_conversation(
        self, *, conversation_id: str, user_id: str, title: str = "新对话"
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        await self.pool.execute(
            """
            INSERT INTO conversations (id, user_id, title, pinned, created_at, updated_at)
            VALUES ($1, $2, $3, FALSE, $4, $5)
            """,
            conversation_id,
            user_id,
            title,
            now,
            now,
        )
        return {
            "id": conversation_id,
            "user_id": user_id,
            "title": title,
            "pinned": False,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

    async def list_conversations(self, user_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT
                c.id,
                c.title,
                c.pinned,
                c.created_at,
                c.updated_at,
                (
                    SELECT t.task_id FROM task_runs t
                    WHERE t.conversation_id = c.id
                    ORDER BY t.created_at DESC LIMIT 1
                ) AS last_task_id,
                (
                    SELECT LEFT(t.task_text, 60) FROM task_runs t
                    WHERE t.conversation_id = c.id
                    ORDER BY t.created_at DESC LIMIT 1
                ) AS preview
            FROM conversations c
            WHERE c.user_id = $1
            ORDER BY c.pinned DESC, c.updated_at DESC
            """,
            user_id,
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append({
                "id": row["id"],
                "title": row["title"],
                "pinned": row["pinned"],
                "created_at": row["created_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat(),
                "last_task_id": row["last_task_id"] or "",
                "preview": row["preview"] or "",
            })
        return result

    async def update_conversation(
        self, conversation_id: str, **fields: Any
    ) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT id, user_id, title, pinned, created_at, updated_at FROM conversations WHERE id = $1",
            conversation_id,
        )
        if row is None:
            return None

        title = fields.get("title", row["title"])
        pinned = fields.get("pinned", row["pinned"])
        now = datetime.now(UTC)

        await self.pool.execute(
            """
            UPDATE conversations
            SET title = $1, pinned = $2, updated_at = $3
            WHERE id = $4
            """,
            title,
            pinned,
            now,
            conversation_id,
        )
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "title": title,
            "pinned": pinned,
            "created_at": row["created_at"].isoformat(),
            "updated_at": now.isoformat(),
        }

    async def delete_conversation(self, conversation_id: str) -> bool:
        result = await self.pool.execute(
            "DELETE FROM conversations WHERE id = $1", conversation_id
        )
        return result == "DELETE 1"

    async def get_conversation_entries(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT e.task_id, e.seq, e.event_type, e.payload, e.created_at
            FROM task_events e
            JOIN task_runs t ON t.task_id = e.task_id
            WHERE t.conversation_id = $1
            ORDER BY e.created_at ASC, e.seq ASC
            """,
            conversation_id,
        )
        entries: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload"])
            entry: dict[str, Any] = {
                "id": f"{row['task_id']}_{row['seq']}",
                "type": row["event_type"],
                "created_at": row["created_at"].isoformat(),
            }
            entry.update(payload)
            entries.append(entry)
        return entries

    async def get_conversation_summary(self, conversation_id: str) -> tuple[str, int]:
        """Return (context_summary, summary_through_seq) for a conversation."""
        row = await self.pool.fetchrow(
            "SELECT context_summary, summary_through_seq FROM conversations WHERE id = $1",
            conversation_id,
        )
        if row is None:
            return "", 0
        return row["context_summary"] or "", row["summary_through_seq"] or 0

    async def update_conversation_summary(
        self, conversation_id: str, summary: str, through_seq: int
    ) -> None:
        """Cache the rolling summary and the seq it covers."""
        await self.pool.execute(
            """
            UPDATE conversations
            SET context_summary = $1, summary_through_seq = $2, updated_at = now()
            WHERE id = $3
            """,
            summary,
            through_seq,
            conversation_id,
        )

    async def get_conversation_primary_events(
        self, conversation_id: str, after_seq: int = 0
    ) -> list[dict[str, Any]]:
        """Fetch user/result/error events for a conversation, ordered chronologically."""
        rows = await self.pool.fetch(
            """
            SELECT e.task_id, e.seq, e.event_type, e.payload, e.created_at
            FROM task_events e
            JOIN task_runs t ON t.task_id = e.task_id
            WHERE t.conversation_id = $1
              AND e.event_type IN ('user', 'result', 'error')
              AND e.seq > $2
            ORDER BY e.created_at ASC, e.seq ASC
            """,
            conversation_id,
            after_seq,
        )
        events: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload"])
            events.append({
                "task_id": row["task_id"],
                "seq": int(row["seq"]),
                "event_type": row["event_type"],
                "content": str(payload.get("content", "")),
                "created_at": row["created_at"].isoformat(),
            })
        return events


class TaskService:
    def __init__(
        self,
        database_url: str,
        redis_url: str,
        dispatcher: TaskDispatcher = dispatch_task,
    ) -> None:
        self._store = TaskRunStore(database_url)
        self._redis_url = redis_url
        self._dispatcher = dispatcher
        self._redis: aioredis.Redis | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def connect(self) -> None:
        await self._store.connect()
        self._redis = aioredis.from_url(self._redis_url, decode_responses=True)

    async def close(self) -> None:
        await self._store.close()
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    @property
    def store(self) -> TaskRunStore:
        return self._store

    async def submit_task(
        self,
        *,
        task_text: str,
        user_id: str,
        mode: str,
        thinking_level: str,
        file_paths: list[str],
        conversation_id: str = "",
    ) -> dict[str, Any]:
        request_payload = {
            "task": task_text,
            "user_id": user_id,
            "mode": mode,
            "thinking_level": thinking_level,
            "file_paths": file_paths,
        }
        snapshot = await self._store.create_task(
            user_id=user_id,
            task_text=task_text,
            mode=mode,
            thinking_level=thinking_level,
            request_payload=request_payload,
            conversation_id=conversation_id,
        )
        background = asyncio.create_task(
            self._execute_task(snapshot["task_id"]),
            name=f"task-runner-{snapshot['task_id']}",
        )
        self._background_tasks.add(background)
        background.add_done_callback(self._background_tasks.discard)
        return snapshot

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await self._store.get_task(task_id)

    async def get_events_after(self, task_id: str, after_seq: int) -> list[dict[str, Any]]:
        return await self._store.get_events_after(task_id, after_seq)

    async def subscribe(self, task_id: str) -> aioredis.client.PubSub:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(f"task:{task_id}:events")
        return pubsub

    async def unsubscribe(self, task_id: str, pubsub: aioredis.client.PubSub) -> None:
        await pubsub.unsubscribe(f"task:{task_id}:events")
        await pubsub.aclose()

    async def _publish(self, task_id: str, event: dict[str, Any]) -> None:
        await self._redis.publish(
            f"task:{task_id}:events", json.dumps(event, ensure_ascii=False)
        )

    async def record_event(
        self, task_id: str, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        event = await self._store.append_event(task_id, event_type, payload)
        await self._publish(task_id, event)
        return event

    async def request_user_input(
        self, task_id: str, question_payload: dict[str, Any]
    ) -> str:
        snapshot = await self._store.get_task(task_id)
        if snapshot is None:
            raise RuntimeError("任务不存在，无法请求用户补充。")

        content = str(question_payload.get("content", "") or "").strip()
        if not content:
            raise ValueError("追问内容不能为空。")

        payload = {
            "content": content,
            "context": str(question_payload.get("context", "") or "").strip(),
            "placeholder": str(question_payload.get("placeholder", "") or "").strip(),
        }

        await self._store.set_waiting_for_user(task_id, payload)
        await self.record_event(task_id, "question", payload)

        result = await self._redis.blpop(f"task:{task_id}:reply", timeout=3600)
        if result is None:
            raise RuntimeError("等待用户回复超时，任务中断。")
        _, answer = result
        return answer

    async def reply_to_task(self, task_id: str, answer: str) -> dict[str, Any]:
        snapshot = await self._store.get_task(task_id)
        if snapshot is None:
            raise KeyError(task_id)

        answer_text = str(answer or "").strip()
        if not answer_text:
            raise ValueError("回复内容不能为空。")

        if snapshot["status"] != "waiting_for_user":
            raise RuntimeError("任务当前不在等待用户回复。")

        await self._store.resume_task(task_id)
        await self.record_event(task_id, "user_reply", {"content": answer_text})
        await self._redis.lpush(f"task:{task_id}:reply", answer_text)
        return await self._store.get_task(task_id) or snapshot

    async def _execute_task(self, task_id: str) -> None:
        snapshot = await self._store.get_task(task_id)
        if snapshot is None:
            return

        task_text = snapshot["task"]
        user_id = snapshot["user_id"]
        mode = snapshot["mode"]
        thinking_level = snapshot["thinking_level"]
        file_paths = snapshot.get("file_paths", [])
        conversation_id = snapshot.get("conversation_id", "")
        execution_task = compose_task_text(task_text, file_paths)
        trace_id = new_trace_id()

        decision = await self._dispatcher(task_text, file_paths, mode, user_id)
        await self._store.set_route_info(
            task_id,
            route_name=decision.route_name,
            route_reason=decision.reason,
            route_confidence=decision.confidence,
        )

        await self._store.mark_started(task_id)
        await self.record_event(task_id, "start", {"content": f"开始执行: {execution_task}"})
        await self.record_event(
            task_id,
            "step",
            {"content": f"🧭 Route: {decision.route_name} ({decision.reason})"},
        )
        log.info("Task received user=%s task=%s", user_id, task_text[:80])

        # Build conversation context
        conversation_context = ""
        if conversation_id:
            try:
                from runtime.conversation_context import ConversationContextBuilder

                ctx = await ConversationContextBuilder(self._store.pool).build(
                    conversation_id, task_text
                )
                conversation_context = ctx.text
                if conversation_context:
                    log.info(
                        "Conversation context built: strategy=%s rounds=%d len=%d",
                        ctx.strategy, ctx.round_count, len(conversation_context),
                    )
            except Exception as exc:
                log.warning("Failed to build conversation context: %s", exc)

        async def on_step(step_info: str) -> None:
            event_type, payload = parse_step_payload(step_info)
            await self.record_event(task_id, event_type, payload)

        async def request_user_input(question_payload: dict[str, Any]) -> str:
            return await self.request_user_input(task_id, question_payload)

        interaction_token = set_task_interaction_handler(request_user_input)
        try:
            set_thinking_level(thinking_level)
            result = await decision.executor(
                execution_task, on_step, user_id=user_id,
                conversation_context=conversation_context,
            )
            await self._store.set_result_text(task_id, result)
            await self.record_event(task_id, "result", {"content": result})
        except Exception as exc:
            error_text = str(exc)
            log.error("Task failed: %s", exc)
            await self._store.set_error_text(task_id, error_text)
            await self.record_event(task_id, "error", {"content": error_text})
            summary = monitor.get_summary(trace_id)
            await self.record_event(task_id, "monitoring", {"content": summary})
            await self._store.finish_task(task_id, "failed")
            monitor.finalize(trace_id)
            return
        finally:
            reset_task_interaction_handler(interaction_token)

        summary = monitor.get_summary(trace_id)
        await self.record_event(task_id, "monitoring", {"content": summary})
        await self._store.finish_task(task_id, "succeeded")
        monitor.finalize(trace_id)

        # Fire-and-forget: generate embeddings for future retrieval
        if conversation_id and self._store.pool:
            try:
                from runtime.conversation_context import generate_embeddings_async

                bg = asyncio.create_task(
                    generate_embeddings_async(self._store.pool, task_id),
                    name=f"embed-{task_id}",
                )
                self._background_tasks.add(bg)
                bg.add_done_callback(self._background_tasks.discard)
            except Exception as exc:
                log.warning("Failed to schedule embedding generation: %s", exc)


def format_sse(event: dict[str, Any]) -> str:
    return (
        f"id: {event['seq']}\n"
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    )
