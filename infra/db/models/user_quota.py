from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from infra.db.base import Base


class UserQuota(Base):
    __tablename__ = "user_quotas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    scope: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)

    daily_task_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weekly_task_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_task_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    daily_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weekly_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    daily_cost_limit_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    weekly_cost_limit_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    monthly_cost_limit_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
