"""SessionRuntime — 独立 Session 层组件。

职责：
1. 追加 canonical 事件 (emit_event) → DB + Redis PubSub
2. 发送 transient 进度 (emit_progress) → 仅 Redis PubSub，不入 DB
3. 暴露历史读取接口 (get_events / get_projection)
4. 提供 wake/recovery 能力
5. 维护 projection (task_runs)
6. 管理 clarification_history 派生

设计原则：
- task_events 是 canonical history（真相源）
- task_runs 是 projection（派生视图）
- checkpoint 是恢复优化资产
- Redis PubSub / SSE 是投递通道
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

from infra.db.repositories.task_event_repo import TaskEventRepository
from infra.db.repositories.task_repo import TaskRunRepository
from infra.db.session import SessionFactory

log = logging.getLogger("chatdada.session")


# ── Event Types ──────────────────────────────────────────────────────────────


class EventType(str, Enum):
    """Canonical event types that enter task_events (the truth source)."""

    # Task lifecycle
    TASK_CREATED = "task_created"
    TASK_STARTED = "start"
    TASK_COMPLETED = "result"
    TASK_FAILED = "error"
    TASK_CANCEL_REQUESTED = "cancel_requested"

    # Routing & planning
    STEP = "step"
    TASK = "task"
    NODE = "node"
    PLAN = "plan"
    BRIEF = "brief"

    # Skill lifecycle
    SKILL_STARTED = "skill_started"
    SKILL_FINISHED = "skill_finished"
    SKILL_FAILED = "skill_failed"

    # Tool lifecycle
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"
    TOOL_CALL_FAILED = "tool_call_failed"

    # Human-in-the-loop
    QUESTION = "question"
    USER_REPLY = "user_reply"

    # Artifacts & checkpoints
    FILE = "file"
    CHECKPOINT = "checkpoint"
    CHECKPOINT_SAVED = "checkpoint_saved"
    STAGE_ARTIFACTS = "stage_artifacts"

    # Observability
    MONITORING = "monitoring"
    REVIEW = "review"


# Well-known transient progress types (never enter DB, no canonical seq)
TRANSIENT_PROGRESS_TYPES = frozenset({
    "token",
    "streaming_content",
    "thinking",
    "dag_progress",
    "result_delta",
    "monitoring_live",
    "custom",
})


# ── Resume Handle ────────────────────────────────────────────────────────────


@dataclass
class ResumeHandle:
    """Encapsulates everything needed to resume a task after crash/restart."""

    task_id: str
    thread_id: str
    stream_input: dict | None = None
    checkpoint_id: str | None = None
    checkpoint_ns: str = ""
    resume_context: dict[str, Any] = field(default_factory=dict)


# ── SessionRuntime ───────────────────────────────────────────────────────────


class SessionRuntime:
    """独立 Session 层组件 — 系统唯一的 durable state boundary。

    Brain/Harness 通过以下接口与 Session 通信：
    - emit_event()         追加 canonical 事件
    - emit_progress()      发送 transient 进度
    - get_events()         按顺序读取历史
    - get_projection()     返回面向查询/UI 的派生视图
    - wake()               恢复被中断的任务
    - record_transition()  状态变更 helper
    - request_cancel()     请求取消任务
    """

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    # ── Canonical Events (DB + Redis PubSub) ─────────────────────────────

    async def emit_event(
        self,
        task_id: str,
        event_type: str | EventType,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """业务事件：入 DB (canonical history) + Redis PubSub。

        参与 /replay、after_seq、Last-Event-ID 恢复。
        """
        et = event_type.value if isinstance(event_type, EventType) else str(event_type)

        async with SessionFactory() as session:
            repo = TaskEventRepository(session)
            row = await repo.append(task_id=task_id, event_type=et, payload=payload)
            await session.commit()

        event: dict[str, Any] = {
            "task_id": task_id,
            "seq": row.seq,
            "type": et,
            "created_at": row.created_at.isoformat(),
        }
        event.update(payload)

        await self._publish(task_id, event)
        return event

    # ── Transient Progress (Redis PubSub only, no DB) ────────────────────

    async def emit_progress(
        self,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """进度提示：仅走 Redis PubSub，不入 DB。

        不分配 canonical seq，不推进 task.last_seq，
        不参与 /replay 或断线重连恢复。

        用于 token, dag_progress, thinking 等高频临时事件。
        """
        event: dict[str, Any] = {
            "task_id": task_id,
            "type": event_type,
            **payload,
        }
        await self._publish(task_id, event)

    # ── History Read ─────────────────────────────────────────────────────

    async def get_events(
        self,
        task_id: str,
        *,
        after_seq: int = 0,
    ) -> list[dict[str, Any]]:
        """按 seq 顺序读取 canonical 事件历史。"""
        async with SessionFactory() as session:
            repo = TaskEventRepository(session)
            rows = await repo.list_after(task_id=task_id, after_seq=after_seq)

        events: list[dict[str, Any]] = []
        for row in rows:
            ev: dict[str, Any] = {
                "task_id": row.task_id,
                "seq": int(row.seq),
                "type": row.event_type,
                "created_at": row.created_at.isoformat(),
            }
            ev.update(dict(row.payload or {}))
            events.append(ev)
        return events

    # ── Projection ───────────────────────────────────────────────────────

    async def get_projection(self, task_id: str) -> dict[str, Any] | None:
        """返回面向查询/UI 的 task_runs 派生视图。"""
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

    # ── State Transitions (helper: emit event + refresh projection) ──────

    async def record_transition(
        self,
        task_id: str,
        new_status: str,
        *,
        reason: str = "",
        error_text: str | None = None,
    ) -> None:
        """Helper: emit canonical status event, then update projection."""
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            row = await repo.get(task_id)
            if row is None:
                return
            now = datetime.now(UTC)
            row.status = new_status
            row.updated_at = now
            if new_status == "running":
                row.started_at = row.started_at or now
                row.pending_question = None
            elif new_status in ("succeeded", "failed", "cancelled"):
                row.finished_at = row.finished_at or now
                if error_text:
                    row.error_text = error_text
            elif new_status == "waiting_for_user":
                pass  # pending_question set separately
            await session.commit()

    async def set_waiting_for_user(
        self, task_id: str, question_payload: dict[str, Any]
    ) -> None:
        """Mark task as waiting_for_user with pending question."""
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.set_waiting_for_user(task_id, question_payload)
            await session.commit()

    async def resume_task(self, task_id: str) -> None:
        """Resume task from waiting_for_user status."""
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.resume_task(task_id)
            await session.commit()

    async def update_projection(self, task_id: str, patch: dict[str, Any]) -> None:
        """Patch task_runs request_payload (projection fields)."""
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.update_request_payload(task_id, patch)
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
        import uuid

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

        return await self.get_projection(task_id) or {
            "task_id": task_id,
            "status": "queued",
            "task": task_text,
            "mode": mode,
            "thinking_level": thinking_level,
        }

    # ── Wake / Recovery ──────────────────────────────────────────────────

    async def wake(self, task_id: str) -> ResumeHandle:
        """在 harness 崩溃、服务重启后恢复执行。

        返回 ResumeHandle，由 Brain/Harness 消费。
        Brain 不对 checkpoint 存储结构做假设。
        """
        projection = await self.get_projection(task_id)
        if projection is None:
            raise ValueError(f"Task {task_id} not found")

        request_payload = projection.get("request_payload", {})
        clarification_history = await self.get_clarification_history(task_id)

        return ResumeHandle(
            task_id=task_id,
            thread_id=task_id,
            stream_input=None,
            checkpoint_id=str(projection.get("latest_checkpoint_id", "") or "") or None,
            resume_context={
                "clarification_history": clarification_history,
                "pending_question": projection.get("pending_question"),
                "nested_interrupt_pending": request_payload.get("nested_interrupt_pending", False),
            },
        )

    async def recover_interrupted_tasks(self) -> list[str]:
        """List task_ids that were interrupted (for process restart recovery)."""
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            return await repo.list_interrupted()

    # ── Cancel ───────────────────────────────────────────────────────────

    async def request_cancel(self, task_id: str) -> None:
        """Signal that a cancellation has been requested.

        Currently writes a Redis key; future: cooperative cancel via session events.
        """
        await self._redis.set(f"cancel:{task_id}", "1", ex=3600)

    async def is_cancel_requested(self, task_id: str) -> bool:
        val = await self._redis.get(f"cancel:{task_id}")
        return val is not None

    # ── Clarification History (derived from events) ──────────────────────

    async def get_clarification_history(
        self, task_id: str
    ) -> list[dict[str, Any]]:
        """从 task_events 中按 seq 重建结构化 clarification_history。

        兼容现有 harness / research resume 格式：
        [{"question": ..., "context": ..., "answer": ..., "checkpoint_id": ..., ...}, ...]

        过渡期：同时从 request_payload 回退读取。
        """
        # 过渡期：从 projection 的 request_payload 读取
        projection = await self.get_projection(task_id)
        if projection is None:
            return []
        rp = projection.get("request_payload", {})
        return list(rp.get("clarification_history", []) or [])

    # ── Internal ─────────────────────────────────────────────────────────

    async def _publish(self, task_id: str, event: dict[str, Any]) -> None:
        await self._redis.publish(
            f"task:{task_id}:events",
            json.dumps(event, ensure_ascii=False),
        )
