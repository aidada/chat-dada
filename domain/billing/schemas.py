from __future__ import annotations

from pydantic import BaseModel


class UserQuotaUpdateRequest(BaseModel):
    enabled: bool = True
    scope: str = "default"
    daily_task_limit: int | None = None
    weekly_task_limit: int | None = None
    monthly_task_limit: int | None = None
    daily_token_limit: int | None = None
    weekly_token_limit: int | None = None
    monthly_token_limit: int | None = None
    daily_cost_limit_usd: float | None = None
    weekly_cost_limit_usd: float | None = None
    monthly_cost_limit_usd: float | None = None


class QuotaPeriodView(BaseModel):
    period: str
    tasks_used: int
    tasks_limit: int | None = None
    tasks_remaining: int | None = None
    tokens_used: int
    tokens_limit: int | None = None
    tokens_remaining: int | None = None
    cost_used_usd: float
    cost_limit_usd: float | None = None
    cost_remaining_usd: float | None = None
    reset_at: str
    blocked_reason: str = ""


class UserQuotaView(BaseModel):
    user_id: str
    scope: str
    enabled: bool
    periods: list[QuotaPeriodView]
    active_block_reason: str = ""

