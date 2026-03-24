from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from infra.db.base import Base


class TaskRun(Base):
    __tablename__ = "task_runs"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    task_text: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String, default="auto", nullable=False)
    thinking_level: Mapped[str] = mapped_column(String, nullable=False)
    route_name: Mapped[str | None] = mapped_column(String, nullable=True)
    route_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    route_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_question: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
