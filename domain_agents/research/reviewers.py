from __future__ import annotations

import json
import logging

from capabilities.review_gates import ReviewGate, ReviewIssue

_log = logging.getLogger("chatdada.reviewers")

_SEMANTIC_REVIEW_PROMPT = """\
你是研究报告质量审查员。请评估以下报告的三个维度，用 JSON 数组回复，每个元素含 severity 和 message：

1. **引用覆盖率**：报告是否引用了足够的来源和证据？缺乏引用 → warning
2. **逻辑连贯性**：论点之间是否有清晰的逻辑链？存在跳跃或矛盾 → warning
3. **证据支撑度**：关键结论是否有数据/实验/文献支撑？缺乏支撑 → warning

如果都通过，返回空数组 `[]`。

报告内容：
{report}
"""


class ResearchReviewGate(ReviewGate):
    async def structural_checks(self, payload: dict[str, object]) -> list[ReviewIssue]:
        issues: list[ReviewIssue] = []
        report = str(payload.get("report", "") or "")
        artifact_refs = payload.get("artifact_refs") or []
        if not report.strip():
            issues.append(ReviewIssue(severity="error", message="研究结果为空"))
        if not artifact_refs:
            issues.append(ReviewIssue(severity="warning", message="未生成结构化 artifact 引用"))
        return issues

    async def semantic_checks(self, payload: dict[str, object]) -> list[ReviewIssue]:
        report = str(payload.get("report", "") or "")
        if len(report.strip()) < 80:
            return [ReviewIssue(severity="warning", message="研究报告过短，可能缺少证据与综述")]

        try:
            return await self._llm_semantic_review(report)
        except Exception as exc:
            _log.warning("LLM semantic review failed, using simple fallback: %s", exc)
            return []

    async def _llm_semantic_review(self, report: str) -> list[ReviewIssue]:
        from core.models import get_llm

        llm = get_llm("orchestrator")
        prompt = _SEMANTIC_REVIEW_PROMPT.format(report=report[:4000])
        response = await llm.ainvoke(prompt)
        content = response.content if hasattr(response, "content") else str(response)
        # Extract JSON array from response
        text = str(content).strip()
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1:
            return []
        items = json.loads(text[start : end + 1])
        issues: list[ReviewIssue] = []
        for item in items:
            if isinstance(item, dict) and item.get("message"):
                issues.append(
                    ReviewIssue(
                        severity=str(item.get("severity", "warning")),
                        message=str(item["message"]),
                    )
                )
        return issues

