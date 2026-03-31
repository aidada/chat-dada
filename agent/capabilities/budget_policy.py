from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


BudgetAction = Literal["allow", "warn", "interrupt", "deny"]


@dataclass(frozen=True)
class BudgetDecision:
    action: BudgetAction
    reason: str


class BudgetPolicy:
    def assess(self, *, estimated_cost: float = 0.0, remaining_budget: float | None = None) -> BudgetDecision:
        if remaining_budget is None:
            return BudgetDecision(action="allow", reason="no explicit budget configured")
        if estimated_cost >= remaining_budget:
            return BudgetDecision(action="interrupt", reason="estimated cost exceeds remaining budget")
        if remaining_budget and estimated_cost >= remaining_budget * 0.8:
            return BudgetDecision(action="warn", reason="estimated cost is close to remaining budget")
        return BudgetDecision(action="allow", reason="within budget")

