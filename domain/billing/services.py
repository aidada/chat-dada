from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging

from apps.web.config import settings
from capabilities.budget_policy import BudgetDecision, BudgetPolicy
from domain.billing.schemas import QuotaPeriodView, UserQuotaUpdateRequest, UserQuotaView
from sqlalchemy.exc import ProgrammingError
from infra.db.repositories.quota_repo import UsageEventRepository, UserQuotaRepository

log = logging.getLogger("chatdada.quota")


class QuotaExceededError(RuntimeError):
    def __init__(self, *, code: str, user_message: str, period: str, metric: str) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message
        self.period = period
        self.metric = metric


@dataclass
class QuotaSnapshot:
    period: str
    tasks_used: int
    tasks_limit: int | None
    tokens_used: int
    tokens_limit: int | None
    cost_used_usd: float
    cost_limit_usd: float | None


class QuotaService:
    def __init__(self, quota_repo: UserQuotaRepository, usage_repo: UsageEventRepository) -> None:
        self.quota_repo = quota_repo
        self.usage_repo = usage_repo
        self.budget_policy = BudgetPolicy()

    async def assess_before_task(self, *, user_id: str, scope: str = "default") -> list[QuotaSnapshot]:
        quota = await self.quota_repo.get_for_user(user_id, scope)
        if quota is None or not quota.enabled:
            return []

        now = datetime.now(UTC)
        periods = {
            "daily": now - timedelta(days=1),
            "weekly": now - timedelta(days=7),
            "monthly": now - timedelta(days=30),
        }
        snapshots: list[QuotaSnapshot] = []
        for period, since in periods.items():
            usage = await self.quota_repo.get_usage_since(user_id=user_id, scope=scope, since=since)
            task_limit = getattr(quota, f"{period}_task_limit")
            token_limit = getattr(quota, f"{period}_token_limit")
            cost_limit = getattr(quota, f"{period}_cost_limit_usd")

            if task_limit is not None and usage.tasks >= task_limit:
                raise QuotaExceededError(
                    code=f"user_{period}_task_limit_exceeded",
                    user_message=f"你的{period.replace('daily', '日').replace('weekly', '周').replace('monthly', '月')}任务额度已用完，请稍后再试。",
                    period=period,
                    metric="tasks",
                )
            if token_limit is not None and usage.total_tokens >= token_limit:
                raise QuotaExceededError(
                    code=f"user_{period}_token_limit_exceeded",
                    user_message=f"你的{period.replace('daily', '日').replace('weekly', '周').replace('monthly', '月')} token 额度已用完，请稍后再试。",
                    period=period,
                    metric="tokens",
                )
            decision: BudgetDecision = self.budget_policy.assess(
                estimated_cost=0.0,
                remaining_budget=(cost_limit - usage.cost_usd) if cost_limit is not None else None,
            )
            if decision.action in {"interrupt", "deny"}:
                raise QuotaExceededError(
                    code=f"user_{period}_cost_limit_exceeded",
                    user_message=f"你的{period.replace('daily', '日').replace('weekly', '周').replace('monthly', '月')}额度已用完，请稍后再试。",
                    period=period,
                    metric="cost",
                )

            snapshots.append(
                QuotaSnapshot(
                    period=period,
                    tasks_used=usage.tasks,
                    tasks_limit=task_limit,
                    tokens_used=usage.total_tokens,
                    tokens_limit=token_limit,
                    cost_used_usd=usage.cost_usd,
                    cost_limit_usd=cost_limit,
                )
            )
        return snapshots

    async def record_task_usage(
        self,
        *,
        user_id: str,
        task_id: str,
        scope: str = "default",
        provider: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        try:
            await self.usage_repo.create(
                user_id=user_id,
                task_id=task_id,
                scope=scope,
                provider=provider,
                model=model,
                total_tokens=total_tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
            )
        except ProgrammingError as exc:
            if "usage_events" in str(exc).lower():
                log.warning("Usage table usage_events is missing; skip usage persistence until migrations are applied.")
                return
            raise

    async def get_user_quota_view(self, *, user_id: str, scope: str = "default") -> UserQuotaView:
        quota = await self.quota_repo.get_for_user(user_id, scope)
        now = datetime.now(UTC)
        periods: list[QuotaPeriodView] = []
        active_block_reason = ""

        for period, since in {
            "daily": now - timedelta(days=1),
            "weekly": now - timedelta(days=7),
            "monthly": now - timedelta(days=30),
        }.items():
            usage = await self.quota_repo.get_usage_since(user_id=user_id, scope=scope, since=since)
            task_limit = getattr(quota, f"{period}_task_limit", None) if quota else None
            token_limit = getattr(quota, f"{period}_token_limit", None) if quota else None
            cost_limit = getattr(quota, f"{period}_cost_limit_usd", None) if quota else None
            blocked_reason = self._build_blocked_reason(
                period=period,
                tasks_used=usage.tasks,
                task_limit=task_limit,
                tokens_used=usage.total_tokens,
                token_limit=token_limit,
                cost_used=usage.cost_usd,
                cost_limit=cost_limit,
            )
            if blocked_reason and not active_block_reason:
                active_block_reason = blocked_reason
            periods.append(
                QuotaPeriodView(
                    period=period,
                    tasks_used=usage.tasks,
                    tasks_limit=task_limit,
                    tasks_remaining=self._remaining(task_limit, usage.tasks),
                    tokens_used=usage.total_tokens,
                    tokens_limit=token_limit,
                    tokens_remaining=self._remaining(token_limit, usage.total_tokens),
                    cost_used_usd=round(usage.cost_usd, 6),
                    cost_limit_usd=cost_limit,
                    cost_remaining_usd=self._remaining_float(cost_limit, usage.cost_usd),
                    reset_at=self._reset_at(period, now).isoformat(),
                    blocked_reason=blocked_reason,
                )
            )

        return UserQuotaView(
            user_id=user_id,
            scope=scope,
            enabled=bool(quota.enabled) if quota else False,
            periods=periods,
            active_block_reason=active_block_reason,
        )

    async def upsert_user_quota(
        self,
        *,
        user_id: str,
        payload: UserQuotaUpdateRequest,
    ) -> UserQuotaView:
        await self.quota_repo.upsert_for_user(
            user_id=user_id,
            scope=payload.scope,
            enabled=payload.enabled,
            daily_task_limit=payload.daily_task_limit,
            weekly_task_limit=payload.weekly_task_limit,
            monthly_task_limit=payload.monthly_task_limit,
            daily_token_limit=payload.daily_token_limit,
            weekly_token_limit=payload.weekly_token_limit,
            monthly_token_limit=payload.monthly_token_limit,
            daily_cost_limit_usd=payload.daily_cost_limit_usd,
            weekly_cost_limit_usd=payload.weekly_cost_limit_usd,
            monthly_cost_limit_usd=payload.monthly_cost_limit_usd,
        )
        await self.quota_repo.session.commit()
        return await self.get_user_quota_view(user_id=user_id, scope=payload.scope)

    def estimate_cost_usd(
        self,
        *,
        model: str,
        total_tokens: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> float:
        pricing = settings.model_pricing.get(model) or settings.model_pricing.get("default") or {}
        if not pricing:
            return 0.0

        total_rate = float(pricing.get("total_per_1m", 0.0) or 0.0)
        input_rate = float(pricing.get("input_per_1m", 0.0) or 0.0)
        output_rate = float(pricing.get("output_per_1m", 0.0) or 0.0)
        if input_tokens or output_tokens:
            return round((input_tokens / 1_000_000) * input_rate + (output_tokens / 1_000_000) * output_rate, 6)
        return round((total_tokens / 1_000_000) * total_rate, 6)

    def estimate_cost_from_usage(self, llm_usage: list[dict]) -> float:
        total = 0.0
        for item in llm_usage:
            total += self.estimate_cost_usd(
                model=str(item.get("model", "") or ""),
                total_tokens=int(item.get("total_tokens", 0) or 0),
                input_tokens=int(item.get("input_tokens", 0) or 0),
                output_tokens=int(item.get("output_tokens", 0) or 0),
            )
        return round(total, 6)

    @staticmethod
    def _remaining(limit: int | None, used: int) -> int | None:
        if limit is None:
            return None
        return max(limit - used, 0)

    @staticmethod
    def _remaining_float(limit: float | None, used: float) -> float | None:
        if limit is None:
            return None
        return round(max(limit - used, 0.0), 6)

    @staticmethod
    def _reset_at(period: str, now: datetime) -> datetime:
        if period == "daily":
            return datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
        if period == "weekly":
            week_start = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=now.weekday())
            return week_start + timedelta(days=7)
        month_anchor = datetime(now.year, now.month, 1, tzinfo=UTC)
        if now.month == 12:
            return datetime(now.year + 1, 1, 1, tzinfo=UTC)
        return datetime(now.year, now.month + 1, 1, tzinfo=UTC)

    @staticmethod
    def _build_blocked_reason(
        *,
        period: str,
        tasks_used: int,
        task_limit: int | None,
        tokens_used: int,
        token_limit: int | None,
        cost_used: float,
        cost_limit: float | None,
    ) -> str:
        period_text = {"daily": "日", "weekly": "周", "monthly": "月"}.get(period, period)
        if task_limit is not None and tasks_used >= task_limit:
            return f"{period_text}任务额度已用完"
        if token_limit is not None and tokens_used >= token_limit:
            return f"{period_text} token 额度已用完"
        if cost_limit is not None and cost_used >= cost_limit:
            return f"{period_text}费用额度已用完"
        return ""
