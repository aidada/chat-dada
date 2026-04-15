from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from infra.db.base import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String, default="新对话", nullable=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    context_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary_through_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
