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
from langgraph.types import Command

from agent_runtime.dispatcher import RouteDecision, build_route_payload, dispatch_task
from agent_runtime.interaction import (
    reset_preloaded_user_replies,
    reset_task_interaction_handler,
    set_preloaded_user_replies,
    set_task_interaction_handler,
)
from agent_runtime.root_graph import build_root_graph
from core.langsmith_config import build_langsmith_run_config
from core.logger import monitor, new_trace_id
from core.models import set_thinking_level
from domain.billing.services import QuotaExceededError, QuotaService
from infra.db.repositories.conversation_repo import ConversationRepository
from infra.db.repositories.quota_repo import UsageEventRepository, UserQuotaRepository
from infra.db.repositories.task_event_repo import TaskEventRepository
from infra.db.repositories.task_repo import TaskRunRepository
from infra.db.session import SessionFactory
from task_platform.streaming import extract_checkpoint_id, translate_stream_part

log = logging.getLogger("chatdada.tasks")

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
HEARTBEAT_INTERVAL_SECONDS = 10

TaskDispatcher = AbcCallable[[str, list[str], str, str], Awaitable[RouteDecision]]


def compose_task_text(task: str, file_paths: list[str]) -> str:
    if not file_paths:
        return task
    file_list = "\n".join(f"  - {path}" for path in file_paths)
    return f"{task}\n\n[用户上传了以下文件，请在任务中使用这些文件]:\n{file_list}"


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _build_attachments(request_payload_raw) -> list[dict[str, Any]]:
    try:
        payload = json.loads(request_payload_raw) if isinstance(request_payload_raw, str) else (request_payload_raw or {})
    except (json.JSONDecodeError, TypeError):
        return []
    file_paths = payload.get("file_paths") or []
    attachments = []
    for fp in file_paths:
        name = Path(fp).name
        is_image = Path(fp).suffix.lower() in _IMAGE_EXTENSIONS
        attachments.append({"name": name, "url": f"/uploads/{name}", "is_image": is_image})
    return attachments


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


def _merge_nested_interrupt_pending(current_pending: bool, payload: dict[str, Any]) -> bool:
    return current_pending or bool(payload.get("nested_graph"))


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
        if not isinstance(self, TaskRunStore) and getattr(self, "pool", None) is not None:
            rows = await self.pool.fetch(
                """
                SELECT task_id
                FROM task_runs
                WHERE status IN ('queued', 'running', 'waiting_for_user')
                ORDER BY created_at ASC
                """
            )
            task_ids = [str(row["task_id"]) for row in rows]
        else:
            async with SessionFactory() as session:
                repo = TaskRunRepository(session)
                task_ids = await repo.list_interrupted()
        if not task_ids:
            return

        log.warning("Recovering %s interrupted task(s) after process restart", len(task_ids))
        for task_id in task_ids:
            snapshot = await self.get_task(task_id) or {}
            if snapshot.get("status") == "waiting_for_user":
                continue
            message = "任务因服务重启而中断，请重新提交。"
            await self.set_error_text(task_id, message)
            await self.append_event(task_id, "error", {"content": message, "recovered": True})
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
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.create(
                task_id=task_id,
                user_id=user_id,
                task_text=task_text,
                mode=mode,
                thinking_level=thinking_level,
                request_payload=request_payload,
                conversation_id=conversation_id,
            )
            if conversation_id:
                await repo.touch_conversation(conversation_id)
            await session.commit()

        return await self.get_task(task_id) or {
            "task_id": task_id,
            "status": "queued",
            "task": task_text,
            "mode": mode,
            "thinking_level": thinking_level,
        }

    async def mark_started(self, task_id: str) -> None:
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.mark_started(task_id)
            await session.commit()

    async def set_waiting_for_user(self, task_id: str, question_payload: dict[str, Any]) -> None:
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.set_waiting_for_user(task_id, question_payload)
            await session.commit()

    async def resume_task(self, task_id: str) -> None:
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.resume_task(task_id)
            await session.commit()

    async def update_request_payload(self, task_id: str, patch: dict[str, Any]) -> None:
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.update_request_payload(task_id, patch)
            await session.commit()

    async def set_route_info(
        self,
        task_id: str,
        *,
        route_name: str,
        route_reason: str,
        route_confidence: float,
    ) -> None:
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.set_route_info(
                task_id,
                route_name=route_name,
                route_reason=route_reason,
                route_confidence=route_confidence,
            )
            await session.commit()

    async def set_result_text(self, task_id: str, result_text: str) -> None:
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.set_result_text(task_id, result_text)
            await session.commit()

    async def set_error_text(self, task_id: str, error_text: str) -> None:
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.set_error_text(task_id, error_text)
            await session.commit()

    async def finish_task(self, task_id: str, status: str) -> None:
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.finish(task_id, status)
            await session.commit()

    async def cancel_task(self, task_id: str, *, error_text: str) -> None:
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.cancel(task_id, error_text=error_text)
            await session.commit()

    async def append_event(
        self, task_id: str, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        async with SessionFactory() as session:
            repo = TaskEventRepository(session)
            row = await repo.append(task_id=task_id, event_type=event_type, payload=payload)
            await session.commit()

        event: dict[str, Any] = {
            "task_id": task_id,
            "seq": row.seq,
            "type": event_type,
            "created_at": row.created_at.isoformat(),
        }
        event.update(payload)
        return event

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            row = await repo.get(task_id)
            if row is None:
                return None
            last_seq = await repo.get_last_seq(task_id)

        payload = dict(row.request_payload or {})
        pending_question = dict(row.pending_question) if row.pending_question else None

        def _ts(v: datetime | None) -> str | None:
            return v.isoformat() if v is not None else None

        return {
            "task_id": row.task_id,
            "user_id": row.user_id,
            "status": row.status,
            "task": row.task_text,
            "mode": row.mode,
            "thinking_level": row.thinking_level,
            "request_payload": payload,
            "route_name": row.route_name,
            "route_reason": row.route_reason,
            "route_confidence": row.route_confidence,
            "file_paths": payload.get("file_paths", []),
            "pending_question": pending_question,
            "result": row.result_text,
            "error": row.error_text,
            "thread_id": payload.get("thread_id", row.task_id),
            "domain": row.route_name or payload.get("domain", ""),
            "artifact_refs": payload.get("artifact_refs", []),
            "interrupt_state": payload.get("interrupt_state"),
            "latest_checkpoint_id": payload.get("latest_checkpoint_id", ""),
            "review": payload.get("review"),
            "budget": payload.get("budget"),
            "created_at": _ts(row.created_at),
            "started_at": _ts(row.started_at),
            "finished_at": _ts(row.finished_at),
            "updated_at": _ts(row.updated_at),
            "conversation_id": row.conversation_id or "",
            "last_seq": last_seq,
        }

    async def get_events_after(self, task_id: str, after_seq: int) -> list[dict[str, Any]]:
        async with SessionFactory() as session:
            repo = TaskEventRepository(session)
            rows = await repo.list_after(task_id=task_id, after_seq=after_seq)
        events: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row.payload or {})
            event: dict[str, Any] = {
                "task_id": row.task_id,
                "seq": int(row.seq),
                "type": row.event_type,
                "created_at": row.created_at.isoformat(),
            }
            event.update(payload)
            events.append(event)
        return events

    async def create_conversation(
        self, *, conversation_id: str, user_id: str, title: str = "新对话"
    ) -> dict[str, Any]:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            row = await repo.create(conversation_id=conversation_id, user_id=user_id, title=title)
            await session.commit()
        return {
            "id": row.id,
            "user_id": row.user_id,
            "title": row.title,
            "pinned": row.pinned,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    async def list_conversations(self, user_id: str) -> list[dict[str, Any]]:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            return await repo.list_for_user(user_id)

    async def update_conversation(
        self, conversation_id: str, **fields: Any
    ) -> dict[str, Any] | None:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            row = await repo.get(conversation_id)
            if row is None:
                return None
            row = await repo.update(row, **fields)
            await session.commit()
        return {
            "id": row.id,
            "user_id": row.user_id,
            "title": row.title,
            "pinned": row.pinned,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            row = await repo.get(conversation_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "user_id": row.user_id,
            "title": row.title,
            "pinned": row.pinned,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    async def delete_conversation(self, conversation_id: str) -> bool:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            row = await repo.get(conversation_id)
            if row is None:
                return False
            await repo.delete(row)
            await session.commit()
            return True

    async def get_conversation_entries(self, conversation_id: str) -> list[dict[str, Any]]:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            return await repo.get_entries(conversation_id)

    async def get_conversation_summary(self, conversation_id: str) -> tuple[str, int]:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            return await repo.get_summary(conversation_id)

    async def update_conversation_summary(
        self, conversation_id: str, summary: str, through_seq: int
    ) -> None:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            await repo.update_summary(conversation_id, summary, through_seq)
            await session.commit()

    async def get_conversation_primary_events(
        self, conversation_id: str, after_seq: int = 0
    ) -> list[dict[str, Any]]:
        async with SessionFactory() as session:
            repo = ConversationRepository(session)
            return await repo.get_primary_events(conversation_id, after_seq)


class TaskService:
    def __init__(
        self,
        database_url: str,
        redis_url: str,
        dispatcher: TaskDispatcher = dispatch_task,
    ) -> None:
        self._store = TaskRunStore(database_url)
        self._database_url = database_url
        self._redis_url = redis_url
        self._dispatcher = dispatcher
        self._redis: aioredis.Redis | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._runner_tasks: dict[str, asyncio.Task[Any]] = {}
        self._checkpointer_cm: Any | None = None
        self._checkpointer: Any | None = None
        self._root_graph: Any | None = None

    async def _open_checkpointer(self) -> Any:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError:
            from langgraph.checkpoint.memory import InMemorySaver

            log.warning("langgraph-checkpoint-postgres not installed; falling back to InMemorySaver")
            return InMemorySaver()

        self._checkpointer_cm = AsyncPostgresSaver.from_conn_string(self._database_url)
        checkpointer = await self._checkpointer_cm.__aenter__()
        await checkpointer.setup()
        return checkpointer

    async def connect(self) -> None:
        await self._store.connect()
        self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        self._checkpointer = await self._open_checkpointer()
        self._root_graph = build_root_graph(
            dispatcher=self._dispatcher,
            checkpointer=self._checkpointer,
        )

    async def close(self) -> None:
        for task in list(self._runner_tasks.values()):
            task.cancel()
        self._runner_tasks.clear()
        await self._store.close()
        if self._redis:
            await self._redis.aclose()
            self._redis = None
        if self._checkpointer_cm is not None:
            await self._checkpointer_cm.__aexit__(None, None, None)
            self._checkpointer_cm = None
            self._checkpointer = None
            self._root_graph = None

    @property
    def store(self) -> TaskRunStore:
        return self._store

    def _track_runner(self, task_id: str, task: asyncio.Task[Any]) -> None:
        self._runner_tasks[task_id] = task
        self._background_tasks.add(task)

        def _cleanup(_task: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(_task)
            if self._runner_tasks.get(task_id) is _task:
                self._runner_tasks.pop(task_id, None)

        task.add_done_callback(_cleanup)

    async def _finalize_cancelled(self, task_id: str, message: str = "任务已取消") -> dict[str, Any] | None:
        snapshot = await self._store.get_task(task_id)
        if snapshot is None:
            return None
        if snapshot.get("status") == "cancelled":
            return snapshot

        await self._store.update_request_payload(
            task_id,
            {"interrupt_state": None, "pending_question": None},
        )
        await self.record_event(
            task_id,
            "task",
            {"phase": "finish", "status": "cancelled", "content": message},
        )
        await self._store.cancel_task(task_id, error_text=message)
        return await self._store.get_task(task_id)

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
        async with SessionFactory() as session:
            quota_service = QuotaService(UserQuotaRepository(session), UsageEventRepository(session))
            try:
                quota_snapshots = await quota_service.assess_before_task(user_id=user_id)
            except QuotaExceededError as exc:
                raise RuntimeError(exc.user_message) from exc

        request_payload = {
            "task": task_text,
            "user_id": user_id,
            "mode": mode,
            "thinking_level": thinking_level,
            "file_paths": file_paths,
            "quota": [
                {
                    "period": item.period,
                    "tasks_used": item.tasks_used,
                    "tasks_limit": item.tasks_limit,
                    "tokens_used": item.tokens_used,
                    "tokens_limit": item.tokens_limit,
                    "cost_used_usd": item.cost_used_usd,
                    "cost_limit_usd": item.cost_limit_usd,
                }
                for item in quota_snapshots
            ],
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
        self._track_runner(snapshot["task_id"], background)
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
        await self._store.update_request_payload(
            task_id,
            {"interrupt_state": payload, "pending_question": payload},
        )
        await self.record_event(task_id, "question", payload)
        raise RuntimeError("request_user_input is now graph-interrupt driven and should not be awaited directly")

    async def reply_to_task(self, task_id: str, answer: str) -> dict[str, Any]:
        snapshot = await self._store.get_task(task_id)
        if snapshot is None:
            raise KeyError(task_id)

        answer_text = str(answer or "").strip()
        if not answer_text:
            raise ValueError("回复内容不能为空。")

        if snapshot["status"] != "waiting_for_user":
            raise RuntimeError("任务当前不在等待用户回复。")

        request_payload = snapshot.get("request_payload", {})
        if not isinstance(request_payload, dict):
            request_payload = {}
        clarification_history = list(request_payload.get("clarification_history", []) or [])
        pending_question = snapshot.get("pending_question") or {}
        if isinstance(pending_question, dict):
            clarification_history.append(
                {
                    "question": str(pending_question.get("content", "") or "").strip(),
                    "context": str(pending_question.get("context", "") or "").strip(),
                    "answer": answer_text,
                    "checkpoint_id": str(pending_question.get("checkpoint_id", "") or "").strip(),
                    "graph_node": str(pending_question.get("graph_node", "") or "").strip(),
                    "nested_graph": str(pending_question.get("nested_graph", "") or "").strip(),
                }
            )

        await self._store.resume_task(task_id)
        await self.record_event(task_id, "user_reply", {"content": answer_text})
        await self._store.update_request_payload(
            task_id,
            {
                "interrupt_state": None,
                "pending_question": None,
                "clarification_history": clarification_history,
            },
        )
        background = asyncio.create_task(
            self._execute_task(task_id, resume_value=answer_text),
            name=f"task-resume-{task_id}",
        )
        self._track_runner(task_id, background)
        return await self._store.get_task(task_id) or snapshot

    async def cancel_running_task(self, task_id: str) -> dict[str, Any]:
        snapshot = await self._store.get_task(task_id)
        if snapshot is None:
            raise KeyError(task_id)
        if task_is_terminal(snapshot["status"]):
            raise RuntimeError("任务已经结束，无法取消。")

        runner = self._runner_tasks.get(task_id)
        if runner is not None:
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
            except Exception:
                log.warning("Runner raised while cancelling task %s", task_id, exc_info=True)

        cancelled = await self._finalize_cancelled(task_id)
        return cancelled or snapshot

    async def _execute_task(self, task_id: str, *, resume_value: str | None = None) -> None:
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
        interrupted = False
        latest_checkpoint_id = snapshot.get("latest_checkpoint_id", "")
        resume_last_step_content = ""
        skipped_resume_replay_step = False

        decision = None
        route_payload = snapshot.get("initial_route_payload")
        if resume_value is None:
            decision = await self._dispatcher(task_text, file_paths, mode, user_id)
            route_payload = build_route_payload(
                task_text=task_text,
                file_paths=file_paths,
                decision=decision,
            )
            public_route_name = route_payload["route_name"]
            await self._store.set_route_info(
                task_id,
                route_name=public_route_name,
                route_reason=route_payload["reason"],
                route_confidence=route_payload["confidence"],
            )
            await self._store.mark_started(task_id)
            await self._store.update_request_payload(
                task_id,
                {
                    "thread_id": task_id,
                    "domain": route_payload["execution_path"],
                    "execution_path": route_payload["execution_path"],
                    "latest_checkpoint_id": "",
                    "interrupt_state": None,
                    "artifact_refs": [],
                    "nested_interrupt_pending": False,
                },
            )
            await self.record_event(task_id, "start", {"content": f"开始执行: {execution_task}"})
            await self.record_event(
                task_id,
                "step",
                {"content": f"🧭 Route: {public_route_name} ({route_payload['reason']})", "thread_id": task_id},
            )
            log.info("Task received user=%s task=%s", user_id, task_text[:80])
        else:
            stored_request = snapshot.get("request_payload", {})
            if not isinstance(stored_request, dict):
                stored_request = {}
            route_payload = {
                "route_name": snapshot.get("route_name", ""),
                "reason": snapshot.get("route_reason", ""),
                "confidence": snapshot.get("route_confidence", 0.0),
                "execution_path": stored_request.get(
                    "execution_path",
                    snapshot.get("route_name", ""),
                ),
            }
            existing_events = await self._store.get_events_after(task_id, 0)
            for event in reversed(existing_events):
                if event["type"] == "step":
                    resume_last_step_content = str(event.get("content", "") or "")
                    break

        conversation_context = ""
        if conversation_id and resume_value is None:
            try:
                from domain.conversations.context import ConversationContextBuilder

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

        interaction_token = set_task_interaction_handler(None)
        preloaded_replies_token = set_preloaded_user_replies(None)
        try:
            set_thinking_level(thinking_level)
            request_payload = snapshot.get("request_payload", {})
            if not isinstance(request_payload, dict):
                request_payload = {}
            nested_interrupt_pending = bool(request_payload.get("nested_interrupt_pending"))
            if resume_value is not None and nested_interrupt_pending:
                replay_replies = [
                    str(item.get("answer", "") or "")
                    for item in list(request_payload.get("clarification_history", []) or [])
                    if isinstance(item, dict)
                    and str(item.get("nested_graph", "") or "").strip()
                    and str(item.get("answer", "") or "").strip()
                ]
                reset_preloaded_user_replies(preloaded_replies_token)
                preloaded_replies_token = set_preloaded_user_replies(replay_replies)
            initial_state = {
                "task_id": task_id,
                "thread_id": task_id,
                "user_id": user_id,
                "mode": mode,
                "thinking_level": thinking_level,
                "task_text": task_text,
                "execution_task": execution_task,
                "file_paths": file_paths,
                "conversation_id": conversation_id,
                "conversation_context": conversation_context,
                "request_payload": dict(request_payload),
                "initial_route_payload": route_payload,
            }
            config = {
                "configurable": {
                    "thread_id": task_id,
                    "nested_interrupt_count": 0,
                    "nested_resume_value": None,
                }
            }
            ls_config = build_langsmith_run_config(
                task_id=task_id,
                user_id=user_id,
                domain=route_payload.get("route_name", ""),
                mode=mode,
            )
            if ls_config:
                config.update(ls_config)
            stream_input: Any
            if resume_value is not None and nested_interrupt_pending:
                stream_input = initial_state
            else:
                stream_input = initial_state if resume_value is None else Command(resume=resume_value)
            current_pending_question: dict[str, Any] | None = None

            async for part in self._root_graph.astream(
                stream_input,
                config=config,
                version="v2",
                stream_mode=["updates", "messages", "custom", "tasks", "checkpoints"],
                subgraphs=True,
            ):
                checkpoint_id = extract_checkpoint_id(part)
                if checkpoint_id:
                    latest_checkpoint_id = checkpoint_id
                    await self._store.update_request_payload(
                        task_id,
                        {"latest_checkpoint_id": checkpoint_id},
                    )
                for event_type, payload in translate_stream_part(
                    part,
                    thread_id=task_id,
                    domain=route_payload["route_name"],
                    checkpoint_id=latest_checkpoint_id,
                    trace_metadata={
                        "trace_id": trace_id,
                        "task_id": task_id,
                        "domain": route_payload["route_name"],
                        "mode": mode,
                    },
                ):
                    if event_type == "question":
                        if (
                            interrupted
                            and not payload.get("nested_graph")
                            and str(payload.get("content", "") or "") == str((current_pending_question or {}).get("content", "") or "")
                        ):
                            continue
                        interrupted = True
                        nested_interrupt_pending = _merge_nested_interrupt_pending(
                            nested_interrupt_pending,
                            payload,
                        )
                        await self._store.set_waiting_for_user(task_id, payload)
                        await self._store.update_request_payload(
                            task_id,
                            {
                                "interrupt_state": payload,
                                "pending_question": payload,
                                "latest_checkpoint_id": latest_checkpoint_id,
                                "nested_interrupt_pending": nested_interrupt_pending,
                            },
                        )
                        current_pending_question = payload
                    if (
                        resume_value is not None
                        and event_type == "step"
                        and not skipped_resume_replay_step
                        and str(payload.get("content", "") or "") == resume_last_step_content
                    ):
                        skipped_resume_replay_step = True
                        continue
                    if (
                        resume_value is not None
                        and event_type == "node"
                        and str(payload.get("node_name", "") or "") in {"run_research", "run_patent", "run_zero_report", "run_ppt"}
                    ):
                        update = payload.get("update") if isinstance(payload.get("update"), dict) else {}
                        update_metadata = payload.get("update_metadata") if isinstance(payload.get("update_metadata"), dict) else {}
                        final_result = str(update.get("final_result", "") or "")
                        if update_metadata.get("cached") and final_result.endswith("未生成最终结果。"):
                            log.warning(
                                "Resume reused cached fallback domain result: task_id=%s node=%s final_result=%s",
                                task_id,
                                payload.get("node_name", ""),
                                final_result,
                            )
                    await self.record_event(task_id, event_type, payload)

            if interrupted:
                summary = monitor.get_summary(trace_id)
                summary.update({"interrupted": True, "waiting_for_user": True})
                await self.record_event(task_id, "monitoring", {"content": summary})
                monitor.finalize(trace_id)
                return

            state_snapshot = await self._root_graph.aget_state(config)
            final_values = getattr(state_snapshot, "values", {}) or {}
            result = str(final_values.get("final_result", "") or "")
            artifact_refs = final_values.get("artifact_refs", []) or []
            review = final_values.get("review") or {}
            budget = final_values.get("budget") or {}
            research_strategy = str(final_values.get("research_strategy", "") or "")
            await self._store.set_result_text(task_id, result)
            payload_patch: dict[str, Any] = {
                "artifact_refs": artifact_refs,
                "interrupt_state": None,
                "pending_question": None,
                "latest_checkpoint_id": latest_checkpoint_id,
                "nested_interrupt_pending": False,
            }
            if review:
                payload_patch["review"] = review
            if budget:
                payload_patch["budget"] = budget
            if research_strategy:
                payload_patch["research_strategy"] = research_strategy
            await self._store.update_request_payload(task_id, payload_patch)
            event_payload: dict[str, Any] = {
                "content": result,
                "artifact_refs": artifact_refs,
                "thread_id": task_id,
            }
            if review:
                event_payload["review"] = review
            if budget:
                event_payload["budget"] = budget
            if research_strategy:
                event_payload["research_strategy"] = research_strategy
            await self.record_event(task_id, "result", event_payload)
        except asyncio.CancelledError:
            log.info("Task cancelled: %s", task_id)
            await self._finalize_cancelled(task_id)
            monitor.finalize(trace_id)
            return
        except Exception as exc:
            error_text = str(exc)
            error_code = "task_execution_error"
            user_message = error_text
            if "weekly_limit_exceeded" in error_text:
                error_code = "provider_weekly_limit_exceeded"
                user_message = "服务端上游模型本周额度已用完，请稍后再试。"
            elif "daily_limit_exceeded" in error_text:
                error_code = "provider_daily_limit_exceeded"
                user_message = "服务端上游模型当日额度已用完，请稍后再试。"
            elif "monthly_limit_exceeded" in error_text:
                error_code = "provider_monthly_limit_exceeded"
                user_message = "服务端上游模型本月额度已用完，请稍后再试。"
            log.error("Task failed: %s", exc)
            await self._store.set_error_text(task_id, error_text)
            await self.record_event(
                task_id,
                "error",
                {
                    "content": user_message,
                    "error_code": error_code,
                    "raw_error": error_text,
                },
            )
            summary = monitor.get_summary(trace_id)
            await self.record_event(task_id, "monitoring", {"content": summary})
            await self._store.finish_task(task_id, "failed")
            monitor.finalize(trace_id)
            return
        finally:
            reset_preloaded_user_replies(preloaded_replies_token)
            reset_task_interaction_handler(interaction_token)

        summary = monitor.get_summary(trace_id)
        await self.record_event(task_id, "monitoring", {"content": summary})
        async with SessionFactory() as session:
            usage_service = QuotaService(UserQuotaRepository(session), UsageEventRepository(session))
            llm_usage = list(summary.get("llm_usage", []) or [])
            total_input_tokens = int(sum(int(item.get("input_tokens", 0) or 0) for item in llm_usage))
            total_output_tokens = int(sum(int(item.get("output_tokens", 0) or 0) for item in llm_usage))
            primary_model = str(llm_usage[0].get("model", "") or "") if llm_usage else ""
            estimated_cost_usd = usage_service.estimate_cost_from_usage(llm_usage)
            await usage_service.record_task_usage(
                user_id=user_id,
                task_id=task_id,
                model=primary_model,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                total_tokens=int(summary.get("total_tokens", 0) or 0),
                cost_usd=estimated_cost_usd,
            )
            await session.commit()
        await self._store.finish_task(task_id, "succeeded")
        monitor.finalize(trace_id)

        if conversation_id and self._store.pool:
            try:
                from domain.conversations.context import generate_embeddings_async

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


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "TaskRunStore",
    "TaskService",
    "compose_task_text",
    "format_sse",
    "parse_step_payload",
    "task_is_terminal",
]
