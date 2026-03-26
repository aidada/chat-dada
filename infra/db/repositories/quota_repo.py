from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging

from sqlalchemy import Select, func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.models.usage_event import UsageEvent
from infra.db.models.user_quota import UserQuota

log = logging.getLogger("chatdada.quota")


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
        try:
            return (await self.session.execute(stmt)).scalar_one_or_none()
        except ProgrammingError as exc:
            if "user_quotas" in str(exc).lower():
                log.warning("Quota table user_quotas is missing; treating quota as disabled until migrations are applied.")
                return None
            raise

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
        try:
            row = (await self.session.execute(stmt)).one()
        except ProgrammingError as exc:
            if "usage_events" in str(exc).lower():
                log.warning("Usage table usage_events is missing; treating usage as zero until migrations are applied.")
                return UsageSummary()
            raise
        return UsageSummary(
            tasks=int(row[0] or 0),
            total_tokens=int(row[1] or 0),
            cost_usd=float(row[2] or 0.0),
        )

    async def upsert_for_user(
        self,
        *,
        user_id: str,
        scope: str,
        enabled: bool,
        daily_task_limit: int | None,
        weekly_task_limit: int | None,
        monthly_task_limit: int | None,
        daily_token_limit: int | None,
        weekly_token_limit: int | None,
        monthly_token_limit: int | None,
        daily_cost_limit_usd: float | None,
        weekly_cost_limit_usd: float | None,
        monthly_cost_limit_usd: float | None,
    ) -> UserQuota:
        row = await self.get_for_user(user_id, scope)
        if row is None:
            row = UserQuota(user_id=user_id, scope=scope)
            self.session.add(row)
        row.enabled = enabled
        row.daily_task_limit = daily_task_limit
        row.weekly_task_limit = weekly_task_limit
        row.monthly_task_limit = monthly_task_limit
        row.daily_token_limit = daily_token_limit
        row.weekly_token_limit = weekly_token_limit
        row.monthly_token_limit = monthly_token_limit
        row.daily_cost_limit_usd = daily_cost_limit_usd
        row.weekly_cost_limit_usd = weekly_cost_limit_usd
        row.monthly_cost_limit_usd = monthly_cost_limit_usd
        row.updated_at = datetime.now(UTC)
        await self.session.flush()
        return row


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
