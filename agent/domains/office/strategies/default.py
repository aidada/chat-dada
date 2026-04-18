from __future__ import annotations

from typing import Any


class DefaultOfficeStrategy:
    """Fallback strategy for non-PPT formats during staged migration."""

    def build_plan(
        self,
        *,
        goal: str,
        requested_slide_count: int,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        title = str(goal or "").replace("\n", " ").strip()[:80] or str(default_create_file or "").rsplit(".", 1)[0] or "Office task"
        return {
            "title": title,
            "slide_count": 0,
            "slides": [],
            "batches": [
                {
                    "index": 0,
                    "slide_start": 0,
                    "slide_end": 0,
                    "slide_titles": [title],
                    "slide_roles": ["document"],
                }
            ],
        }

    def summarize_plan(self, plan: dict[str, Any]) -> str:
        return f"- plan_title: {str(plan.get('title', '') or '').strip()}\n- build_batches: 1"

    def validate_plan(
        self,
        *,
        plan: dict[str, Any],
        goal: str,
        requested_slide_count: int,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        issues: list[str] = []
        if not isinstance(plan, dict):
            issues.append("plan_not_dict")
            plan = {}

        title = str(plan.get("title", "") or "").strip()
        if not title:
            issues.append("missing_title")
            title = str(goal or "").replace("\n", " ").strip()[:80] or str(default_create_file or "").rsplit(".", 1)[0] or "Office task"

        batches = plan.get("batches")
        if not isinstance(batches, list) or not batches:
            issues.append("missing_batches")
            batches = [{
                "index": 0,
                "slide_start": 0,
                "slide_end": 0,
                "slide_titles": [title],
                "slide_roles": ["document"],
            }]

        normalized = {
            "title": title,
            "slide_count": 0,
            "slides": [],
            "batches": list(batches),
        }
        return normalized, issues

    def build_phase_guidance(
        self,
        *,
        plan: dict[str, Any],
        current_batch_index: int,
        repair_mode: bool,
        qa_feedback: str,
    ) -> str:
        if repair_mode:
            guidance = [
                "- 当前阶段: repair run",
                "- 本轮只允许针对上一轮 QA 问题做有限修复。",
                "- 修复完成后，执行完整 QA。",
            ]
            if qa_feedback:
                guidance.extend(["- 必须优先修复这些问题：", qa_feedback])
            return "\n".join(guidance)
        return "- 当前阶段: build\n- 当前格式未使用 slide 批次规划，按单批文档构建执行。"

    def build_input_sections(
        self,
        *,
        goal: str,
        operation: str,
        format_hint: str,
        runtime_target: str,
        default_create_file: str,
        requested_slide_count: int | None,
        build_batch_size: int,
        source_files: list[str],
        context: str,
        qa_feedback: str,
        plan: dict[str, Any],
        current_batch_index: int,
        repair_mode: bool,
        merged_constraints: dict[str, Any] | None = None,
    ) -> list[str]:
        sections = [
            goal,
            "",
            "执行上下文：",
            f"- operation: {operation}",
            f"- format: {format_hint}",
            f"- runtime_target: {runtime_target}",
        ]
        if default_create_file:
            sections.append(f"- default_create_file: {default_create_file}")
        sections.append(f"- build_batch_size: {build_batch_size}")
        sections.append("- plan_summary:")
        sections.append(self.summarize_plan(plan))
        sections.append(f"- current_batch_index: {current_batch_index}")
        sections.append(f"- repair_mode: {str(repair_mode).lower()}")
        if source_files:
            sections.append("- source_files:")
            sections.extend(f"  - {item}" for item in source_files)
        if context:
            sections.extend(["", "已有上下文：", context])
        if qa_feedback:
            sections.extend(["", "上轮 QA 未通过，必须先修正这些问题：", qa_feedback])
        return sections

    def evaluate_quality_stats(
        self,
        *,
        operation: str,
        stats: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return []

    def advance_after_build(
        self,
        *,
        plan: dict[str, Any],
        current_batch_index: int,
        repair_mode: bool,
        completed_pages: int,
    ) -> dict[str, Any]:
        if repair_mode:
            return {
                "current_batch_index": current_batch_index,
                "completed_pages": completed_pages,
                "next_stage": "qa_fix",
            }
        return {
            "current_batch_index": current_batch_index + 1,
            "completed_pages": completed_pages,
            "next_stage": "qa_fix",
        }
