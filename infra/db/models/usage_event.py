from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from infra.db.base import Base


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    task_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    scope: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    provider: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    capability: Mapped[str] = mapped_column(String(64), default="task", nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
