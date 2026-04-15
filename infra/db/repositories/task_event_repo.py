from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import json

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.models.task_event import TaskEvent
from infra.db.models.task_run import TaskRun


class TaskEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(self, *, task_id: str, event_type: str, payload: dict[str, Any]) -> TaskEvent:
        task_row = (
            await self.session.execute(
                select(TaskRun)
                .where(TaskRun.task_id == task_id)
                .with_for_update()
            )
        ).scalar_one_or_none()

        next_seq = await self.session.scalar(
            select(func.coalesce(func.max(TaskEvent.seq), 0) + 1).where(TaskEvent.task_id == task_id)
        )
        created_at = datetime.now(UTC)
        row = TaskEvent(
            task_id=task_id,
            seq=int(next_seq or 1),
            event_type=event_type,
            payload=payload,
            created_at=created_at,
            embedding=None,
        )
        self.session.add(row)

        if task_row is not None:
            task_row.updated_at = created_at

        await self.session.flush()
        return row

    async def list_after(self, *, task_id: str, after_seq: int) -> list[TaskEvent]:
        stmt: Select = (
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.seq > after_seq)
            .order_by(TaskEvent.seq.asc())
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows)

    async def get_last_seq(self, task_id: str) -> int:
        value = await self.session.scalar(
            select(func.coalesce(func.max(TaskEvent.seq), 0)).where(TaskEvent.task_id == task_id)
        )
        return int(value or 0)

    async def list_pending_embeddings(self, *, task_id: str) -> list[TaskEvent]:
        stmt: Select = (
            select(TaskEvent)
            .where(
                TaskEvent.task_id == task_id,
                TaskEvent.event_type.in_(("lifecycle.completed", "lifecycle.failed")),
                TaskEvent.embedding.is_(None),
            )
            .order_by(TaskEvent.seq.asc())
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows)

    async def set_embedding(self, *, task_id: str, seq: int, embedding: str | list[float]) -> None:
        stmt: Select = select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.seq == seq)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return
        if isinstance(embedding, str):
            cleaned = embedding.strip().removeprefix("[").removesuffix("]")
            row.embedding = [float(item) for item in cleaned.split(",") if item.strip()]
        else:
            row.embedding = embedding
        await self.session.flush()
