from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from capabilities.review_gates import (
    ReviewDimension,
    ReviewGate,
    ReviewIssue,
    ReviewResult,
    RevisionTarget,
)
from domain_agents.research.config import get_deliverable_profile


class ResearchReviewGate(ReviewGate):
    async def evaluate(self, payload: dict[str, Any]) -> ReviewResult:
        issues = await self.structural_checks(payload)
        dimensions = await self.dimension_checks(payload)
        revision_targets = await self.build_revision_targets(payload, issues, dimensions)

        needs_replan = any(
            dimension.name == "intent_alignment" and dimension.score < 0.45
            for dimension in dimensions
        )
        has_errors = any(issue.severity == "error" for issue in issues)
        passed = (not has_errors) and (not needs_replan) and all(dimension.passed for dimension in dimensions)

        targeted_modules = {target.module_id for target in revision_targets}
        module_outputs = payload.get("module_outputs") or {}
        lock_modules = [
            module_id
            for module_id, draft in module_outputs.items()
            if str(draft.get("content", "") or "").strip() and module_id not in targeted_modules
        ]

        summary = "评审通过，可进入最终整合。" if passed else "评审未通过，需要定向修订低分模块。"
        return ReviewResult(
            passed=passed,
            issues=issues,
            dimensions=dimensions,
            revision_targets=revision_targets,
            needs_replan=needs_replan,
            lock_modules=lock_modules,
            summary=summary,
            user_feedback_required=(not passed) or needs_replan,
        )

    async def structural_checks(self, payload: dict[str, Any]) -> list[ReviewIssue]:
        issues: list[ReviewIssue] = []
        report = str(payload.get("report", "") or "").strip()
        module_outputs = payload.get("module_outputs") or {}
        evidence_bank = payload.get("evidence_bank") or []

        if not report:
            issues.append(ReviewIssue(severity="error", message="聚合草案为空"))
        if not module_outputs:
            issues.append(ReviewIssue(severity="error", message="尚未生成任何模块草案"))
        if not evidence_bank:
            issues.append(ReviewIssue(severity="warning", message="当前没有可追溯 evidence 记录"))
        return issues

    async def dimension_checks(self, payload: dict[str, Any]) -> list[ReviewDimension]:
        brief = payload.get("brief") or {}
        report = str(payload.get("report", "") or "")
        module_outputs = payload.get("module_outputs") or {}
        evidence_bank = payload.get("evidence_bank") or []
        profile = get_deliverable_profile(brief.get("deliverable_type"))
        current_year = datetime.now(UTC).year

        required_modules = set(profile.required_modules)
        present_modules = {
            module_id
            for module_id, draft in module_outputs.items()
            if str(draft.get("content", "") or "").strip()
        }
        evidence_count = len(evidence_bank)
        traceable_count = sum(1 for item in evidence_bank if item.get("traceable") or item.get("url"))
        coverage_ratio = len(present_modules & required_modules) / max(len(required_modules), 1)
        recency_years = [int(item["year"]) for item in evidence_bank if item.get("year")]
        recent_items = [year for year in recency_years if year >= current_year - 5]

        method_text = " ".join(
            str((module_outputs.get(module_id) or {}).get("content", "") or "")
            for module_id in ("method_candidates", "experiment_design")
        ).lower()
        argument_text = " ".join(
            str((module_outputs.get(module_id) or {}).get("content", "") or "")
            for module_id in ("argument_map", "contributions", "limitations")
        ).lower()

        dimensions: list[ReviewDimension] = []

        citation_trace_score = 0.2
        if traceable_count >= 5:
            citation_trace_score = 0.9
        elif traceable_count >= 3:
            citation_trace_score = 0.78
        elif traceable_count >= 1:
            citation_trace_score = 0.58
        dimensions.append(
            ReviewDimension(
                name="citation_authenticity_traceability",
                score=citation_trace_score,
                passed=citation_trace_score >= 0.65,
                strengths=["存在可追溯来源"] if traceable_count else [],
                weaknesses=[] if traceable_count else ["缺少可追溯引用或 URL"],
                affected_modules=["related_work", "problem_definition"],
            )
        )

        citation_coverage_score = min(0.35 + coverage_ratio * 0.65, 1.0)
        if evidence_count < 2:
            citation_coverage_score = min(citation_coverage_score, 0.5)
        dimensions.append(
            ReviewDimension(
                name="citation_relevance_coverage",
                score=citation_coverage_score,
                passed=citation_coverage_score >= 0.68,
                strengths=["核心模块基本覆盖"] if coverage_ratio >= 0.8 else [],
                weaknesses=["关键模块覆盖不足或证据过少"] if coverage_ratio < 0.8 or evidence_count < 2 else [],
                affected_modules=["related_work", "problem_definition"],
            )
        )

        if not evidence_bank:
            citation_recency_score = 0.2
        elif recency_years:
            citation_recency_score = 0.85 if recent_items else 0.45
        else:
            citation_recency_score = 0.6
        dimensions.append(
            ReviewDimension(
                name="citation_recency",
                score=citation_recency_score,
                passed=citation_recency_score >= 0.55,
                strengths=["覆盖了近 5 年文献"] if recent_items else [],
                weaknesses=["缺少近 5 年文献覆盖"] if evidence_bank and not recent_items and recency_years else [],
                affected_modules=["related_work"],
            )
        )

        method_required = "method_candidates" in required_modules
        method_keywords = ("variable", "metric", "dataset", "baseline", "指标", "变量", "数据集", "baseline", "ablation")
        method_hits = sum(1 for token in method_keywords if token in method_text)
        methodological_score = 0.8 if not method_required else min(0.35 + method_hits * 0.12, 0.92)
        dimensions.append(
            ReviewDimension(
                name="methodological_rigor",
                score=methodological_score,
                passed=methodological_score >= 0.65,
                strengths=["方法模块包含操作化细节"] if method_hits >= 3 else [],
                weaknesses=["方法步骤、变量或指标不够具体"] if method_required and method_hits < 3 else [],
                affected_modules=["method_candidates"],
            )
        )

        experiment_required = "experiment_design" in required_modules
        experiment_keywords = ("dataset", "benchmark", "baseline", "metric", "ablation", "误差", "实验", "评价")
        experiment_hits = sum(1 for token in experiment_keywords if token in method_text)
        experiment_score = 0.8 if not experiment_required else min(0.35 + experiment_hits * 0.1, 0.9)
        dimensions.append(
            ReviewDimension(
                name="experimental_feasibility",
                score=experiment_score,
                passed=experiment_score >= 0.65,
                strengths=["实验设计有较清晰的落地路径"] if experiment_hits >= 3 else [],
                weaknesses=["实验流程、baseline 或指标仍不够可执行"] if experiment_required and experiment_hits < 3 else [],
                affected_modules=["experiment_design"],
            )
        )

        argument_keywords = ("空白", "gap", "motivation", "贡献", "局限", "risk", "claim", "背景")
        argument_hits = sum(1 for token in argument_keywords if token in argument_text)
        argument_score = min(0.35 + argument_hits * 0.08, 0.92)
        dimensions.append(
            ReviewDimension(
                name="argument_chain_completeness",
                score=argument_score,
                passed=argument_score >= 0.68,
                strengths=["论证链基本闭环"] if argument_hits >= 4 else [],
                weaknesses=["背景-空白-方法-贡献-局限链条仍不完整"] if argument_hits < 4 else [],
                affected_modules=["argument_map", "contributions", "limitations"],
            )
        )

        section_hits = sum(1 for section in profile.final_sections[: min(4, len(profile.final_sections))] if section in report)
        intent_score = min(0.4 + section_hits * 0.15, 0.95)
        if brief.get("deliverable_type") == "paper_guidance" and "论文" not in report and "introduction" not in report.lower():
            intent_score = min(intent_score, 0.48)
        dimensions.append(
            ReviewDimension(
                name="intent_alignment",
                score=intent_score,
                passed=intent_score >= 0.65,
                strengths=["输出结构与目标产物匹配"] if section_hits >= 2 else [],
                weaknesses=["当前草案与目标产物或用户偏好不够对齐"] if section_hits < 2 else [],
                affected_modules=[module["module_id"] for module in payload.get("plan", {}).get("modules", [])],
            )
        )

        return dimensions

    async def build_revision_targets(
        self,
        payload: dict[str, Any],
        issues: list[ReviewIssue],
        dimensions: list[ReviewDimension],
    ) -> list[RevisionTarget]:
        targets: dict[str, RevisionTarget] = {}

        def _upsert(
            module_id: str,
            reason: str,
            *,
            priority: str = "medium",
            actions: list[str] | None = None,
            preserve_constraints: list[str] | None = None,
            requires_new_evidence: bool = False,
        ) -> None:
            existing = targets.get(module_id)
            merged_actions = actions or []
            merged_preserve = preserve_constraints or ["保持其他锁定模块不变"]
            if existing is not None:
                existing.actions = list(dict.fromkeys([*existing.actions, *merged_actions]))
                existing.preserve_constraints = list(dict.fromkeys([*existing.preserve_constraints, *merged_preserve]))
                existing.reason = f"{existing.reason}; {reason}"
                existing.requires_new_evidence = existing.requires_new_evidence or requires_new_evidence
                if priority == "high":
                    existing.priority = "high"
                return
            targets[module_id] = RevisionTarget(
                module_id=module_id,
                reason=reason,
                priority=priority,
                actions=merged_actions,
                preserve_constraints=merged_preserve,
                requires_new_evidence=requires_new_evidence,
            )

        for dimension in dimensions:
            if dimension.passed:
                continue
            if dimension.name == "citation_authenticity_traceability":
                _upsert(
                    "related_work",
                    "引用真实性与可追溯性不足",
                    priority="high",
                    actions=["补充可追溯 URL、论文题目、作者或来源信息"],
                    requires_new_evidence=True,
                )
            elif dimension.name == "citation_relevance_coverage":
                _upsert(
                    "related_work",
                    "相关工作覆盖不足",
                    priority="high",
                    actions=["补充关键方向和代表性工作，明确研究空白"],
                    requires_new_evidence=True,
                )
            elif dimension.name == "citation_recency":
                _upsert(
                    "related_work",
                    "近年文献覆盖不足",
                    actions=["补充近 3-5 年关键文献，区分经典工作与近年进展"],
                    requires_new_evidence=True,
                )
            elif dimension.name == "methodological_rigor":
                _upsert(
                    "method_candidates",
                    "方法论不够严谨",
                    priority="high",
                    actions=["补充变量、数据、流程、指标与方法选择理由"],
                )
            elif dimension.name == "experimental_feasibility":
                _upsert(
                    "experiment_design",
                    "实验设计不可执行或细节不足",
                    priority="high",
                    actions=["明确数据来源、baseline、评价指标、ablation 与误差分析"],
                )
            elif dimension.name == "argument_chain_completeness":
                _upsert(
                    "argument_map",
                    "论证链不完整",
                    actions=["补足研究背景、问题、空白、方法、贡献之间的逻辑闭环"],
                )
                _upsert(
                    "limitations",
                    "局限性与风险刻画不足",
                    actions=["明确不能写过头的点和当前证据边界"],
                )
            elif dimension.name == "intent_alignment":
                _upsert(
                    "problem_definition",
                    "当前草案与用户目标或产物类型不够对齐",
                    priority="high",
                    actions=["重新校准任务定义、输出重点与最终产物结构"],
                )

        for issue in issues:
            if issue.severity == "error" and "空" in issue.message:
                _upsert(
                    "problem_definition",
                    issue.message,
                    priority="high",
                    actions=["先生成基本模块草案，再进入评估与整合"],
                )

        return list(targets.values())
