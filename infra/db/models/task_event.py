from __future__ import annotations

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Integer, JSON, PrimaryKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from infra.db.base import Base


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (PrimaryKeyConstraint("task_id", "seq", name="pk_task_events"),)

    task_id: Mapped[str] = mapped_column(String)
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
