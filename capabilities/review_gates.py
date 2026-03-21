from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReviewIssue:
    severity: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewResult:
    passed: bool
    issues: list[ReviewIssue] = field(default_factory=list)


class ReviewGate:
    async def structural_checks(self, payload: dict[str, Any]) -> list[ReviewIssue]:
        return []

    async def semantic_checks(self, payload: dict[str, Any]) -> list[ReviewIssue]:
        return []

    async def evaluate(self, payload: dict[str, Any]) -> ReviewResult:
        issues = [*await self.structural_checks(payload), *await self.semantic_checks(payload)]
        has_errors = any(issue.severity == "error" for issue in issues)
        return ReviewResult(passed=not has_errors, issues=issues)

