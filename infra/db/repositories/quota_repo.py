from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.models.usage_event import UsageEvent
from infra.db.models.user_quota import UserQuota


@dataclass
class UsageSummary:
    tasks: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


class UserQuotaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_for_user(self, user_id: str, scope: str = "default") -> UserQuota | None:
        stmt: Select = select(UserQuota).where(UserQuota.user_id == user_id, UserQuota.scope == scope)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_usage_since(self, *, user_id: str, scope: str, since: datetime) -> UsageSummary:
        stmt: Select = (
            select(
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.total_tokens), 0),
                func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
            )
            .where(
                UsageEvent.user_id == user_id,
                UsageEvent.scope == scope,
                UsageEvent.created_at >= since,
            )
        )
        row = (await self.session.execute(stmt)).one()
        return UsageSummary(
            tasks=int(row[0] or 0),
            total_tokens=int(row[1] or 0),
            cost_usd=float(row[2] or 0.0),
        )


class UsageEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        user_id: str,
        task_id: str,
        scope: str = "default",
        provider: str = "",
        model: str = "",
        capability: str = "task",
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> UsageEvent:
        row = UsageEvent(
            user_id=user_id,
            task_id=task_id,
            scope=scope,
            provider=provider,
            model=model,
            capability=capability,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
        )
        self.session.add(row)
        await self.session.flush()
        return row
