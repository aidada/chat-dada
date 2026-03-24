from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from capabilities.budget_policy import BudgetDecision, BudgetPolicy
from infra.db.repositories.quota_repo import UsageEventRepository, UserQuotaRepository


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
        total_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        await self.usage_repo.create(
            user_id=user_id,
            task_id=task_id,
            scope=scope,
            provider=provider,
            model=model,
            total_tokens=total_tokens,
            input_tokens=0,
            output_tokens=0,
            cost_usd=cost_usd,
        )
