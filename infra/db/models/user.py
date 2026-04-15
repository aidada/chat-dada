from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    avatar_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    oauth_accounts = relationship("OAuthAccount", back_populates="user")
    sessions = relationship("UserSession", back_populates="user")
