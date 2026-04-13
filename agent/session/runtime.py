"""SessionRuntime — 独立 Session 层组件。

职责：
1. 追加 canonical 事件 (emit_event) → DB + Redis PubSub
2. 发送 transient 进度 (emit_progress) → 仅 Redis PubSub，不入 DB
3. 暴露历史读取接口 (get_events / get_projection)
4. 提供 wake/recovery 能力
5. 维护 projection (task_runs)
6. 管理 clarification_history 派生

协议事件类型由 agent.session.protocol 统一定义（Layer 1 后端实现）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

from agent.session.protocol import (
    CANONICAL_EVENT_TYPES,
    TRANSIENT_EVENT_TYPES,
    EventType,
    is_transient,
)
from domain.tasks.session_store import SessionStore, TaskProjectionRecord

log = logging.getLogger("chatdada.session")

# 向后兼容别名 — 旧代码直接导入这些符号仍可工作
is_transient_progress_type = is_transient

RUNTIME_REQUEST_PAYLOAD_KEYS = frozenset(
    {
        "artifact_refs",
        "budget",
        "cancel_state",
        "clarification_history",
        "interrupt_state",
        "latest_checkpoint_id",
        "nested_interrupt_pending",
        "pending_question",
        "review",
    }
)


@dataclass
class ResumeHandle:
    """Encapsulates everything needed to resume a task after crash/restart."""

    task_id: str
    thread_id: str
    stream_input: dict | None = None
    checkpoint_id: str | None = None
    checkpoint_ns: str = ""
    resume_context: dict[str, Any] = field(default_factory=dict)


class SessionRuntime:
    """独立 Session 层组件 — 系统唯一的 durable state boundary。"""

    def __init__(self, redis: aioredis.Redis, store: SessionStore) -> None:
        self._redis = redis
        self._store = store

    async def setup(self) -> None:
        await self._store.setup()

    async def emit_event(
        self,
        task_id: str,
        event_type: str | EventType,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """发送 canonical 事件：入 DB + Redis PubSub，携带单调递增 seq。

        envelope 格式（符合 Layer 0 spec）：
            { type, taskId, timestamp, seq, payload: {...} }
        """
        et = event_type.value if isinstance(event_type, EventType) else str(event_type)
        if et not in CANONICAL_EVENT_TYPES:
            raise ValueError(f"{et!r} 不是 canonical 事件类型")

        row = await self._store.append_event(task_id=task_id, event_type=et, payload=payload)
        event: dict[str, Any] = {
            "type":        et,
            "taskId":      task_id,
            "timestamp":   row.created_at.isoformat(),
            "seq":         row.seq,
            "stream_kind": "canonical",
            "payload":     dict(row.payload or {}),
        }
        await self._publish(task_id, event)
        return event

    async def emit_progress(
        self,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """发送 transient 进度事件：仅 Redis PubSub，不入 DB，无 seq。

        envelope 格式（符合 Layer 0 spec）：
            { type, taskId, timestamp, payload: {...} }
        """
        event: dict[str, Any] = {
            "type":        str(event_type),
            "taskId":      task_id,
            "timestamp":   datetime.now(UTC).isoformat(),
            "stream_kind": "transient",
            "payload":     payload,
        }
        await self._publish(task_id, event)

    async def get_events(
        self,
        task_id: str,
        *,
        after_seq: int = 0,
    ) -> list[dict[str, Any]]:
        """从 DB 读取历史 canonical 事件，以新 envelope 格式返回。"""
        rows = await self._store.list_events_after(task_id=task_id, after_seq=after_seq)
        return [
            {
                "type":      row.event_type,
                "taskId":    row.task_id,
                "timestamp": row.created_at.isoformat(),
                "seq":       int(row.seq),
                "payload":   dict(row.payload or {}),
            }
            for row in rows
        ]

    async def get_projection(self, task_id: str) -> dict[str, Any] | None:
        projection = await self._store.get_projection(task_id)
        if projection is None:
            return None
        return self._projection_to_dict(projection)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await self.get_projection(task_id)

    async def get_events_after(self, task_id: str, after_seq: int) -> list[dict[str, Any]]:
        return await self.get_events(task_id, after_seq=after_seq)

    async def record_transition(
        self,
        task_id: str,
        new_status: str,
        *,
        reason: str = "",
        error_text: str | None = None,
        cancel_state: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        patch: dict[str, Any] = {
            "status": new_status,
            "cancel_state": cancel_state,
        }
        if new_status == "running":
            patch["started_at"] = now
            patch["pending_question"] = None
        elif new_status in ("succeeded", "failed", "cancelled"):
            patch["finished_at"] = now
            patch["pending_question"] = None
        if error_text is not None:
            patch["error_text"] = error_text
        await self._store.update_projection(task_id, projection_patch=patch)
        if reason:
            log.info("task transition: %s -> %s (%s)", task_id, new_status, reason)

    async def set_waiting_for_user(
        self,
        task_id: str,
        question_payload: dict[str, Any],
        *,
        latest_checkpoint_id: str = "",
        nested_interrupt_pending: bool = False,
    ) -> None:
        await self._store.update_projection(
            task_id,
            projection_patch={
                "status": "waiting_for_user",
                "pending_question": question_payload,
                "latest_checkpoint_id": latest_checkpoint_id,
                "nested_interrupt_pending": nested_interrupt_pending,
                "cancel_state": None,
            },
            clear_request_payload_keys=RUNTIME_REQUEST_PAYLOAD_KEYS,
        )

    async def resume_task(self, task_id: str) -> None:
        await self._store.update_projection(
            task_id,
            projection_patch={
                "status": "running",
                "pending_question": None,
                "cancel_state": None,
            },
            clear_request_payload_keys=("interrupt_state", "pending_question"),
        )

    async def update_projection(
        self,
        task_id: str,
        *,
        projection_patch: dict[str, Any] | None = None,
        request_payload_patch: dict[str, Any] | None = None,
        clear_request_payload_keys: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        projection = await self._store.update_projection(
            task_id,
            projection_patch=projection_patch,
            request_payload_patch=request_payload_patch,
            clear_request_payload_keys=clear_request_payload_keys,
        )
        if projection is None:
            return None
        return self._projection_to_dict(projection)

    async def set_result_text(self, task_id: str, result_text: str) -> None:
        await self._store.update_projection(
            task_id,
            projection_patch={"result_text": result_text, "error_text": None},
        )

    async def set_error_text(self, task_id: str, error_text: str) -> None:
        await self._store.update_projection(
            task_id,
            projection_patch={"error_text": error_text},
        )

    async def set_route_info(
        self,
        task_id: str,
        *,
        route_name: str,
        route_reason: str,
        route_confidence: float,
    ) -> None:
        await self._store.update_projection(
            task_id,
            projection_patch={
                "route_name": route_name,
                "route_reason": route_reason,
                "route_confidence": route_confidence,
            },
        )

    async def mark_started(self, task_id: str) -> None:
        await self.record_transition(task_id, "running")

    async def finish_task(self, task_id: str, status: str) -> None:
        await self._store.update_projection(
            task_id,
            projection_patch={
                "status": status,
                "finished_at": datetime.now(UTC),
                "pending_question": None,
                "cancel_state": None if status in ("succeeded", "failed", "cancelled") else None,
            },
        )

    async def cancel_task(self, task_id: str, *, error_text: str) -> None:
        await self._store.update_projection(
            task_id,
            projection_patch={
                "status": "cancelled",
                "error_text": error_text,
                "pending_question": None,
                "finished_at": datetime.now(UTC),
                "cancel_state": None,
            },
            clear_request_payload_keys=("interrupt_state", "pending_question"),
        )

    async def update_request_payload(self, task_id: str, patch: dict[str, Any]) -> None:
        projection_patch, request_payload_patch = self._split_payload_patch(dict(patch or {}))
        clear_runtime_keys = tuple(
            key for key in RUNTIME_REQUEST_PAYLOAD_KEYS if key in request_payload_patch
        )
        if clear_runtime_keys:
            for key in clear_runtime_keys:
                request_payload_patch.pop(key, None)
        await self._store.update_projection(
            task_id,
            projection_patch=projection_patch,
            request_payload_patch=request_payload_patch or None,
            clear_request_payload_keys=clear_runtime_keys,
        )

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
        projection = await self._store.create_task(
            user_id=user_id,
            task_text=task_text,
            mode=mode,
            thinking_level=thinking_level,
            request_payload={
                key: value
                for key, value in dict(request_payload or {}).items()
                if key not in RUNTIME_REQUEST_PAYLOAD_KEYS
            },
            conversation_id=conversation_id,
        )
        return self._projection_to_dict(projection)

    async def wake(self, task_id: str) -> ResumeHandle:
        projection = await self.get_projection(task_id)
        if projection is None:
            raise ValueError(f"Task {task_id} not found")

        clarification_history = await self.get_clarification_history(task_id)
        return ResumeHandle(
            task_id=task_id,
            thread_id=task_id,
            stream_input=None,
            checkpoint_id=str(projection.get("latest_checkpoint_id", "") or "") or None,
            resume_context={
                "clarification_history": clarification_history,
                "pending_question": projection.get("pending_question"),
                "nested_interrupt_pending": bool(projection.get("nested_interrupt_pending")),
            },
        )

    async def recover_interrupted_tasks(self) -> list[str]:
        return await self._store.list_interrupted_task_ids()

    async def request_cancel(self, task_id: str) -> None:
        await self._redis.set(f"cancel:{task_id}", "1", ex=3600)

    async def is_cancel_requested(self, task_id: str) -> bool:
        return await self._redis.get(f"cancel:{task_id}") is not None

    async def get_clarification_history(self, task_id: str) -> list[dict[str, Any]]:
        events = await self.get_events(task_id, after_seq=0)
        history: list[dict[str, Any]] = []
        for event in events:
            # 从新格式 envelope 中取 payload
            payload = event.get("payload") or {}
            if event["type"] == EventType.INTERACTION_QUESTION.value:
                history.append(
                    {
                        "question": str(payload.get("content", "") or "").strip(),
                        "context": str(payload.get("context", "") or "").strip(),
                        "answer": "",
                        "checkpoint_id": str(payload.get("checkpoint_id", "") or "").strip(),
                        "graph_node": str(payload.get("graph_node", "") or "").strip(),
                        "nested_graph": str(payload.get("nested_graph", "") or "").strip(),
                    }
                )
                continue
            if event["type"] != EventType.INTERACTION_ANSWER.value:
                continue
            answer = str(payload.get("content", "") or "").strip()
            if history and not history[-1].get("answer"):
                history[-1]["answer"] = answer
            else:
                history.append(
                    {
                        "question": "",
                        "context": "",
                        "answer": answer,
                        "checkpoint_id": "",
                        "graph_node": "",
                        "nested_graph": "",
                    }
                )

        if history:
            return history

        projection = await self._store.get_projection(task_id)
        if projection is None:
            return []
        return list((projection.request_payload or {}).get("clarification_history", []) or [])

    def _projection_to_dict(self, projection: TaskProjectionRecord) -> dict[str, Any]:
        def _ts(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        request_payload = dict(projection.request_payload or {})
        return {
            "task_id": projection.task_id,
            "user_id": projection.user_id,
            "status": projection.status,
            "task": projection.task_text,
            "mode": projection.mode,
            "thinking_level": projection.thinking_level,
            "request_payload": request_payload,
            "route_name": projection.route_name,
            "route_reason": projection.route_reason,
            "route_confidence": projection.route_confidence,
            "file_paths": list(request_payload.get("file_paths", []) or []),
            "pending_question": dict(projection.pending_question) if projection.pending_question else None,
            "result": projection.result_text,
            "error": projection.error_text,
            "thread_id": projection.task_id,
            "domain": projection.route_name or str(request_payload.get("domain", "") or ""),
            "artifact_refs": list(projection.artifact_refs or []),
            "latest_checkpoint_id": projection.latest_checkpoint_id,
            "nested_interrupt_pending": bool(projection.nested_interrupt_pending),
            "review": dict(projection.review) if projection.review else None,
            "budget": dict(projection.budget) if projection.budget else None,
            "cancel_state": projection.cancel_state,
            "created_at": _ts(projection.created_at),
            "started_at": _ts(projection.started_at),
            "finished_at": _ts(projection.finished_at),
            "updated_at": _ts(projection.updated_at),
            "conversation_id": projection.conversation_id or "",
            "last_seq": int(projection.last_seq or 0),
        }

    def _split_payload_patch(
        self,
        patch: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        projection_patch: dict[str, Any] = {}
        request_payload_patch = dict(patch)
        for key in (
            "artifact_refs",
            "budget",
            "cancel_state",
            "latest_checkpoint_id",
            "nested_interrupt_pending",
            "pending_question",
            "review",
        ):
            if key in request_payload_patch:
                projection_patch[key] = request_payload_patch.pop(key)
        request_payload_patch.pop("interrupt_state", None)
        return projection_patch, request_payload_patch

    async def _publish(self, task_id: str, event: dict[str, Any]) -> None:
        await self._redis.publish(
            f"task:{task_id}:events",
            json.dumps(event, ensure_ascii=False),
        )
