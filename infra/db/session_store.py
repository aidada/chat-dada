from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Sequence

from sqlalchemy import text

from domain.tasks.session_store import SessionStore, TaskEventRecord, TaskProjectionRecord
from infra.db.models.task_run import TaskRun
from infra.db.repositories.task_event_repo import TaskEventRepository
from infra.db.repositories.task_repo import TaskRunRepository
from infra.db.session import SessionFactory


class PostgresSessionStore(SessionStore):
    def __init__(self, session_factory=SessionFactory) -> None:
        self._session_factory = session_factory

    async def setup(self) -> None:
        statements = (
            """
            ALTER TABLE task_runs
            ADD COLUMN IF NOT EXISTS artifact_refs JSONB NOT NULL DEFAULT '[]'::jsonb
            """,
            """
            ALTER TABLE task_runs
            ADD COLUMN IF NOT EXISTS latest_checkpoint_id TEXT NOT NULL DEFAULT ''
            """,
            """
            ALTER TABLE task_runs
            ADD COLUMN IF NOT EXISTS nested_interrupt_pending BOOLEAN NOT NULL DEFAULT FALSE
            """,
            """
            ALTER TABLE task_runs
            ADD COLUMN IF NOT EXISTS review JSONB
            """,
            """
            ALTER TABLE task_runs
            ADD COLUMN IF NOT EXISTS budget JSONB
            """,
            """
            ALTER TABLE task_runs
            ADD COLUMN IF NOT EXISTS cancel_state TEXT
            """,
            # ── 协议迁移：旧扁平类型名 → category.action 格式 ──────────────────────
            # WHERE event_type NOT LIKE '%.%' 使语句幂等：
            #   已迁移的行（包含 '.'）跳过，避免重复更新。
            """
            UPDATE task_events
            SET event_type = CASE event_type
                WHEN 'start'              THEN 'lifecycle.started'
                WHEN 'task_created'       THEN 'lifecycle.started'
                WHEN 'result'             THEN 'lifecycle.completed'
                WHEN 'error'              THEN 'lifecycle.failed'
                WHEN 'cancel_requested'   THEN 'lifecycle.cancelled'
                WHEN 'question'           THEN 'interaction.question'
                WHEN 'user_reply'         THEN 'interaction.answer'
                WHEN 'file'               THEN 'artifact.created'
                WHEN 'stage_artifacts'    THEN 'artifact.staged'
                WHEN 'step'               THEN 'progress.step'
                WHEN 'task'               THEN 'progress.step'
                WHEN 'task_start'         THEN 'progress.step'
                WHEN 'task_complete'      THEN 'progress.step'
                WHEN 'task_dag'           THEN 'progress.dag'
                WHEN 'node'               THEN 'progress.node'
                WHEN 'plan'               THEN 'progress.plan'
                WHEN 'brief'              THEN 'progress.brief'
                WHEN 'checkpoint'         THEN 'progress.checkpoint'
                WHEN 'checkpoint_saved'   THEN 'progress.checkpoint'
                WHEN 'monitoring'         THEN 'system.monitoring'
                WHEN 'monitoring_live'    THEN 'system.monitoring'
                WHEN 'tool_call_started'  THEN 'tool.started'
                WHEN 'tool_call_finished' THEN 'tool.completed'
                WHEN 'tool_call_failed'   THEN 'tool.failed'
                WHEN 'skill_started'      THEN 'tool.started'
                WHEN 'skill_finished'     THEN 'tool.completed'
                WHEN 'skill_failed'       THEN 'tool.failed'
                ELSE event_type
            END
            WHERE event_type NOT LIKE '%%.%%'
            """,
        )
        async with self._session_factory() as session:
            for statement in statements:
                await session.execute(text(statement))
            await session.commit()

    async def append_event(
        self,
        *,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> TaskEventRecord:
        async with self._session_factory() as session:
            repo = TaskEventRepository(session)
            row = await repo.append(task_id=task_id, event_type=event_type, payload=payload)
            await session.commit()
            return TaskEventRecord(
                task_id=row.task_id,
                seq=int(row.seq),
                event_type=row.event_type,
                payload=dict(row.payload or {}),
                created_at=row.created_at,
            )

    async def list_events_after(
        self,
        *,
        task_id: str,
        after_seq: int,
    ) -> list[TaskEventRecord]:
        async with self._session_factory() as session:
            repo = TaskEventRepository(session)
            rows = await repo.list_after(task_id=task_id, after_seq=after_seq)
            return [
                TaskEventRecord(
                    task_id=row.task_id,
                    seq=int(row.seq),
                    event_type=row.event_type,
                    payload=dict(row.payload or {}),
                    created_at=row.created_at,
                )
                for row in rows
            ]

    async def create_task(
        self,
        *,
        user_id: str,
        task_text: str,
        mode: str,
        thinking_level: str,
        request_payload: dict[str, Any],
        conversation_id: str = "",
    ) -> TaskProjectionRecord:
        import uuid

        task_id = f"task_{uuid.uuid4().hex[:12]}"
        async with self._session_factory() as session:
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
        projection = await self.get_projection(task_id)
        if projection is None:
            raise RuntimeError(f"failed to create task projection for {task_id}")
        return projection

    async def get_projection(self, task_id: str) -> TaskProjectionRecord | None:
        async with self._session_factory() as session:
            repo = TaskRunRepository(session)
            row = await repo.get(task_id)
            if row is None:
                return None
            last_seq = await repo.get_last_seq(task_id)
            return self._to_projection(row, last_seq=last_seq)

    async def list_interrupted_task_ids(self) -> list[str]:
        async with self._session_factory() as session:
            repo = TaskRunRepository(session)
            return await repo.list_interrupted()

    async def update_projection(
        self,
        task_id: str,
        *,
        projection_patch: dict[str, Any] | None = None,
        request_payload_patch: dict[str, Any] | None = None,
        clear_request_payload_keys: Sequence[str] = (),
    ) -> TaskProjectionRecord | None:
        async with self._session_factory() as session:
            repo = TaskRunRepository(session)
            row = await repo.get(task_id)
            if row is None:
                return None

            for key, value in dict(projection_patch or {}).items():
                if not hasattr(row, key):
                    raise AttributeError(f"unknown task_runs projection field: {key}")
                setattr(row, key, value)

            if request_payload_patch or clear_request_payload_keys:
                merged = dict(row.request_payload or {})
                for key in clear_request_payload_keys:
                    merged.pop(str(key), None)
                if request_payload_patch:
                    merged.update(request_payload_patch)
                row.request_payload = merged

            row.updated_at = datetime.now(UTC)
            await session.commit()
            last_seq = await repo.get_last_seq(task_id)
            return self._to_projection(row, last_seq=last_seq)

    def _to_projection(self, row: TaskRun, *, last_seq: int) -> TaskProjectionRecord:
        return TaskProjectionRecord(
            task_id=row.task_id,
            user_id=row.user_id,
            status=row.status,
            task_text=row.task_text,
            mode=row.mode,
            thinking_level=row.thinking_level,
            request_payload=dict(row.request_payload or {}),
            route_name=row.route_name,
            route_reason=row.route_reason,
            route_confidence=row.route_confidence,
            result_text=row.result_text,
            error_text=row.error_text,
            pending_question=dict(row.pending_question) if row.pending_question else None,
            conversation_id=row.conversation_id or "",
            artifact_refs=list(row.artifact_refs or []),
            latest_checkpoint_id=str(row.latest_checkpoint_id or ""),
            nested_interrupt_pending=bool(row.nested_interrupt_pending),
            review=dict(row.review) if row.review else None,
            budget=dict(row.budget) if row.budget else None,
            cancel_state=row.cancel_state,
            created_at=row.created_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
            updated_at=row.updated_at,
            last_seq=int(last_seq or 0),
        )
