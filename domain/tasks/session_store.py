from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence


@dataclass(slots=True)
class TaskEventRecord:
    task_id: str
    seq: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(slots=True)
class TaskProjectionRecord:
    task_id: str
    user_id: str
    status: str
    task_text: str
    mode: str
    thinking_level: str
    request_payload: dict[str, Any] = field(default_factory=dict)
    route_name: str | None = None
    route_reason: str | None = None
    route_confidence: float | None = None
    result_text: str | None = None
    error_text: str | None = None
    pending_question: dict[str, Any] | None = None
    conversation_id: str = ""
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    latest_checkpoint_id: str = ""
    nested_interrupt_pending: bool = False
    review: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    cancel_state: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None
    last_seq: int = 0


class SessionStore(Protocol):
    async def setup(self) -> None: ...

    async def append_event(
        self,
        *,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> TaskEventRecord: ...

    async def list_events_after(
        self,
        *,
        task_id: str,
        after_seq: int,
    ) -> list[TaskEventRecord]: ...

    async def create_task(
        self,
        *,
        user_id: str,
        task_text: str,
        mode: str,
        thinking_level: str,
        request_payload: dict[str, Any],
        conversation_id: str = "",
    ) -> TaskProjectionRecord: ...

    async def get_projection(self, task_id: str) -> TaskProjectionRecord | None: ...

    async def list_interrupted_task_ids(self) -> list[str]: ...

    async def update_projection(
        self,
        task_id: str,
        *,
        projection_patch: dict[str, Any] | None = None,
        request_payload_patch: dict[str, Any] | None = None,
        clear_request_payload_keys: Sequence[str] = (),
    ) -> TaskProjectionRecord | None: ...
