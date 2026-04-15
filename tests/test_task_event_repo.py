from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from infra.db.models.task_event import TaskEvent
from infra.db.models.task_run import TaskRun
from infra.db.repositories.task_event_repo import TaskEventRepository
from infra.db.repositories.task_repo import TaskRunRepository
from infra.db.session import SessionFactory


@pytest.mark.asyncio
async def test_task_event_append_serializes_seq_under_concurrency():
    task_id = f"task_repo_{uuid.uuid4().hex[:12]}"

    try:
        async with SessionFactory() as session:
            repo = TaskRunRepository(session)
            await repo.create(
                task_id=task_id,
                user_id="user_concurrent",
                task_text="concurrency test",
                mode="agent",
                thinking_level="medium",
                request_payload={},
            )
            await session.commit()
    except (OSError, PermissionError, SQLAlchemyError) as exc:
        pytest.skip(f"database unavailable for concurrency test: {exc}")

    async def append_one(index: int) -> int:
        async with SessionFactory() as session:
            repo = TaskEventRepository(session)
            row = await repo.append(
                task_id=task_id,
                event_type="progress.step",
                payload={"content": f"step-{index}"},
            )
            await session.commit()
            return int(row.seq)

    try:
        seqs = await asyncio.gather(*(append_one(index) for index in range(12)))
        assert len(set(seqs)) == 12
        assert sorted(seqs) == list(range(1, 13))

        async with SessionFactory() as session:
            rows = (
                await session.execute(
                    select(TaskEvent.seq)
                    .where(TaskEvent.task_id == task_id)
                    .order_by(TaskEvent.seq.asc())
                )
            ).scalars().all()
            assert list(rows) == list(range(1, 13))
    finally:
        async with SessionFactory() as session:
            await session.execute(delete(TaskEvent).where(TaskEvent.task_id == task_id))
            await session.execute(delete(TaskRun).where(TaskRun.task_id == task_id))
            await session.commit()
