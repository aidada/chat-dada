from __future__ import annotations

from agent.capabilities.review_gates import ReviewGate, ReviewIssue
from agent.domains.zero_report.schemas import ActionMatrix, Timeline


class ZeroReportReviewGate(ReviewGate):
    async def structural_checks(self, payload: dict[str, object]) -> list[ReviewIssue]:
        issues: list[ReviewIssue] = []
        timeline = Timeline.model_validate(payload.get("timeline") or {})
        actions = ActionMatrix.model_validate(payload.get("action_matrix") or {})

        if not timeline.events:
            issues.append(ReviewIssue(severity="error", message="缺少时间线事件"))
        for event in timeline.events:
            if not event.timestamp.strip():
                issues.append(ReviewIssue(severity="error", message="时间线事件缺少时间戳"))
        if not actions.items:
            issues.append(ReviewIssue(severity="error", message="缺少整改行动项"))
        for item in actions.items:
            if not item.owner.strip():
                issues.append(ReviewIssue(severity="error", message="行动项缺少责任人"))
            if not item.due_date.strip():
                issues.append(ReviewIssue(severity="error", message="行动项缺少截止时间"))
        return issues

    async def semantic_checks(self, payload: dict[str, object]) -> list[ReviewIssue]:
        issues: list[ReviewIssue] = []
        root_cause = (payload.get("root_cause_tree") or {}).get("root") or {}
        if not root_cause:
            issues.append(ReviewIssue(severity="warning", message="根因树为空，因果链可能未闭环"))
        if len((payload.get("timeline") or {}).get("events", [])) < 2:
            issues.append(ReviewIssue(severity="warning", message="时间线过短，事件完整性不足"))
        return issues

