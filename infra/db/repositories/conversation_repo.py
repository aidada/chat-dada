from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.models.conversation import Conversation
from infra.db.models.task_event import TaskEvent
from infra.db.models.task_run import TaskRun


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, *, conversation_id: str, user_id: str, title: str = "新对话") -> Conversation:
        row = Conversation(
            id=conversation_id,
            user_id=user_id,
            title=title,
            pinned=False,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, conversation_id: str) -> Conversation | None:
        return await self.session.get(Conversation, conversation_id)

    async def update(self, conversation: Conversation, **fields: Any) -> Conversation:
        if "title" in fields:
            conversation.title = fields["title"]
        if "pinned" in fields:
            conversation.pinned = fields["pinned"]
        conversation.updated_at = datetime.now(UTC)
        await self.session.flush()
        return conversation

    async def delete(self, conversation: Conversation) -> None:
        await self.session.delete(conversation)
        await self.session.flush()

    async def get_summary(self, conversation_id: str) -> tuple[str, int]:
        row = await self.get(conversation_id)
        if row is None:
            return "", 0
        return row.context_summary or "", row.summary_through_seq or 0

    async def update_summary(self, conversation_id: str, summary: str, through_seq: int) -> None:
        row = await self.get(conversation_id)
        if row is None:
            return
        row.context_summary = summary
        row.summary_through_seq = through_seq
        row.updated_at = datetime.now(UTC)
        await self.session.flush()

    async def get_primary_events(self, conversation_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        stmt: Select = (
            select(TaskEvent.task_id, TaskEvent.seq, TaskEvent.event_type, TaskEvent.payload, TaskEvent.created_at)
            .join(TaskRun, TaskRun.task_id == TaskEvent.task_id)
            .where(
                TaskRun.conversation_id == conversation_id,
                TaskEvent.event_type.in_(("user", "result", "error")),
                TaskEvent.seq > after_seq,
            )
            .order_by(TaskEvent.created_at.asc(), TaskEvent.seq.asc())
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "task_id": row.task_id,
                "seq": int(row.seq),
                "event_type": row.event_type,
                "content": str((row.payload or {}).get("content", "")),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        last_task_subq = (
            select(TaskRun.task_id)
            .where(TaskRun.conversation_id == Conversation.id)
            .order_by(desc(TaskRun.created_at))
            .limit(1)
            .scalar_subquery()
        )
        preview_subq = (
            select(func.left(TaskRun.task_text, 60))
            .where(TaskRun.conversation_id == Conversation.id)
            .order_by(desc(TaskRun.created_at))
            .limit(1)
            .scalar_subquery()
        )

        stmt: Select = (
            select(
                Conversation.id,
                Conversation.title,
                Conversation.pinned,
                Conversation.created_at,
                Conversation.updated_at,
                last_task_subq.label("last_task_id"),
                preview_subq.label("preview"),
            )
            .where(Conversation.user_id == user_id)
            .order_by(desc(Conversation.pinned), desc(Conversation.updated_at))
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "id": row.id,
                "title": row.title,
                "pinned": row.pinned,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
                "last_task_id": row.last_task_id or "",
                "preview": row.preview or "",
            }
            for row in rows
        ]

    async def get_rounds(self, conversation_id: str) -> list[dict[str, str]]:
        task_rows = (
            await self.session.execute(
                select(TaskRun.task_id, TaskRun.task_text, TaskRun.created_at)
                .where(
                    TaskRun.conversation_id == conversation_id,
                    TaskRun.status.in_(("succeeded", "failed", "running", "queued")),
                )
                .order_by(TaskRun.created_at.asc())
            )
        ).all()
        if not task_rows:
            return []

        task_ids = [str(row.task_id) for row in task_rows]
        event_rows = (
            await self.session.execute(
                select(TaskEvent.task_id, TaskEvent.seq, TaskEvent.payload)
                .where(
                    TaskEvent.task_id.in_(task_ids),
                    TaskEvent.event_type == "result",
                )
                .order_by(TaskEvent.task_id.asc(), TaskEvent.seq.desc())
            )
        ).all()

        latest_results: dict[str, str] = {}
        for row in event_rows:
            task_id = str(row.task_id)
            if task_id in latest_results:
                continue
            payload = dict(row.payload or {})
            latest_results[task_id] = str(payload.get("content", "") or "")

        return [
            {
                "task_id": str(row.task_id),
                "task_text": row.task_text or "",
                "result_content": latest_results.get(str(row.task_id), ""),
            }
            for row in task_rows
        ]

    async def get_entries(self, conversation_id: str) -> list[dict[str, Any]]:
        task_rows = (
            await self.session.execute(
                select(TaskRun.task_id, TaskRun.task_text, TaskRun.request_payload, TaskRun.created_at)
                .where(TaskRun.conversation_id == conversation_id)
                .order_by(TaskRun.created_at.asc())
            )
        ).all()
        event_rows = (
            await self.session.execute(
                select(TaskEvent.task_id, TaskEvent.seq, TaskEvent.event_type, TaskEvent.payload, TaskEvent.created_at)
                .join(TaskRun, TaskRun.task_id == TaskEvent.task_id)
                .where(TaskRun.conversation_id == conversation_id)
                .order_by(TaskEvent.created_at.asc(), TaskEvent.seq.asc())
            )
        ).all()

        events_by_task: dict[str, list[Any]] = {}
        for row in event_rows:
            events_by_task.setdefault(row.task_id, []).append(row)

        entries: list[dict[str, Any]] = []
        for task_row in task_rows:
            payload = dict(task_row.request_payload or {})
            attachments = []
            for fp in payload.get("file_paths", []) or []:
                name = str(fp).split("/")[-1]
                attachments.append({"name": name, "url": f"/uploads/{name}", "is_image": False})

            entries.append(
                {
                    "id": f"{task_row.task_id}_user",
                    "type": "user",
                    "content": task_row.task_text,
                    "attachments": attachments,
                    "created_at": task_row.created_at.isoformat(),
                }
            )

            for row in events_by_task.get(task_row.task_id, []):
                entry = {
                    "id": f"{row.task_id}_{row.seq}",
                    "task_id": row.task_id,
                    "type": row.event_type,
                    "created_at": row.created_at.isoformat(),
                }
                entry.update(dict(row.payload or {}))
                entries.append(entry)
        return entries
