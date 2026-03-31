from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReviewIssue:
    """最基础的问题项，适合表达结构错误或通用告警。"""

    severity: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewDimension:
    """单个评估维度的打分结果。"""

    name: str
    score: float
    passed: bool
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    affected_modules: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RevisionTarget:
    """评估后需要定向返工的模块。"""

    module_id: str
    reason: str
    priority: str = "medium"
    actions: list[str] = field(default_factory=list)
    preserve_constraints: list[str] = field(default_factory=list)
    requires_new_evidence: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewResult:
    """统一评审结果。

    兼容简单任务的 issue 列表，也支持复杂工作流需要的维度打分、
    定向修订目标、重规划信号和模块锁定信息。
    """

    passed: bool
    issues: list[ReviewIssue] = field(default_factory=list)
    dimensions: list[ReviewDimension] = field(default_factory=list)
    revision_targets: list[RevisionTarget] = field(default_factory=list)
    needs_replan: bool = False
    lock_modules: list[str] = field(default_factory=list)
    summary: str = ""
    user_feedback_required: bool = False


class ReviewGate:
    """通用评审门禁基类。"""

    async def structural_checks(self, payload: dict[str, Any]) -> list[ReviewIssue]:
        return []

    async def semantic_checks(self, payload: dict[str, Any]) -> list[ReviewIssue]:
        return []

    async def dimension_checks(self, payload: dict[str, Any]) -> list[ReviewDimension]:
        return []

    async def build_revision_targets(
        self,
        payload: dict[str, Any],
        issues: list[ReviewIssue],
        dimensions: list[ReviewDimension],
    ) -> list[RevisionTarget]:
        return []

    async def evaluate(self, payload: dict[str, Any]) -> ReviewResult:
        # 默认实现只负责把三个层级的结果汇总起来。
        # 更复杂的领域（例如 research）可以直接覆写 evaluate。
        issues = [
            *await self.structural_checks(payload),
            *await self.semantic_checks(payload),
        ]
        dimensions = await self.dimension_checks(payload)
        revision_targets = await self.build_revision_targets(payload, issues, dimensions)
        has_errors = any(issue.severity == "error" for issue in issues)
        return ReviewResult(
            passed=not has_errors,
            issues=issues,
            dimensions=dimensions,
            revision_targets=revision_targets,
        )
