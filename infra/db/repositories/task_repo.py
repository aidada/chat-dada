from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.models.task_run import TaskRun


class TaskRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, task_id: str) -> TaskRun | None:
        return await self.session.get(TaskRun, task_id)

    async def get_task_text(self, task_id: str) -> str:
        row = await self.get(task_id)
        if row is None:
            return ""
        return row.task_text or ""

    async def get_last_seq(self, task_id: str) -> int:
        from infra.db.models.task_event import TaskEvent

        value = await self.session.scalar(
            select(func.coalesce(func.max(TaskEvent.seq), 0)).where(TaskEvent.task_id == task_id)
        )
        return int(value or 0)

    async def list_interrupted(self) -> list[str]:
        rows = (
            await self.session.execute(
                select(TaskRun.task_id)
                .where(TaskRun.status.in_(("queued", "running", "waiting_for_user")))
                .order_by(TaskRun.created_at.asc())
            )
        ).all()
        return [str(row.task_id) for row in rows]

    async def create(
        self,
        *,
        task_id: str,
        user_id: str,
        task_text: str,
        mode: str,
        thinking_level: str,
        request_payload: dict,
        conversation_id: str = "",
    ) -> TaskRun:
        row = TaskRun(
            task_id=task_id,
            user_id=user_id,
            status="queued",
            task_text=task_text,
            mode=mode,
            thinking_level=thinking_level,
            request_payload=request_payload,
            conversation_id=conversation_id or None,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def touch_conversation(self, conversation_id: str) -> None:
        from infra.db.models.conversation import Conversation

        row = await self.session.get(Conversation, conversation_id)
        if row is None:
            return
        row.updated_at = datetime.now(UTC)
        await self.session.flush()

    async def mark_started(self, task_id: str) -> None:
        row = await self.get(task_id)
        if row is None:
            return
        now = datetime.now(UTC)
        row.status = "running"
        row.started_at = row.started_at or now
        row.pending_question = None
        row.updated_at = now
        await self.session.flush()

    async def set_waiting_for_user(self, task_id: str, question_payload: dict[str, Any]) -> None:
        row = await self.get(task_id)
        if row is None:
            return
        now = datetime.now(UTC)
        row.status = "waiting_for_user"
        row.pending_question = question_payload
        row.updated_at = now
        await self.session.flush()

    async def resume_task(self, task_id: str) -> None:
        row = await self.get(task_id)
        if row is None:
            return
        now = datetime.now(UTC)
        row.status = "running"
        row.pending_question = None
        row.updated_at = now
        await self.session.flush()

    async def update_request_payload(self, task_id: str, patch: dict[str, Any]) -> None:
        row = await self.get(task_id)
        if row is None:
            return
        merged = dict(row.request_payload or {})
        merged.update(patch)
        row.request_payload = merged
        row.updated_at = datetime.now(UTC)
        await self.session.flush()

    async def set_route_info(
        self,
        task_id: str,
        *,
        route_name: str,
        route_reason: str,
        route_confidence: float,
    ) -> None:
        row = await self.get(task_id)
        if row is None:
            return
        row.route_name = route_name
        row.route_reason = route_reason
        row.route_confidence = route_confidence
        row.updated_at = datetime.now(UTC)
        await self.session.flush()

    async def set_result_text(self, task_id: str, result_text: str) -> None:
        row = await self.get(task_id)
        if row is None:
            return
        row.result_text = result_text
        row.error_text = None
        row.updated_at = datetime.now(UTC)
        await self.session.flush()

    async def set_error_text(self, task_id: str, error_text: str) -> None:
        row = await self.get(task_id)
        if row is None:
            return
        row.error_text = error_text
        row.updated_at = datetime.now(UTC)
        await self.session.flush()

    async def finish(self, task_id: str, status: str) -> None:
        row = await self.get(task_id)
        if row is None:
            return
        now = datetime.now(UTC)
        row.status = status
        row.finished_at = now
        row.pending_question = None
        row.updated_at = now
        await self.session.flush()
