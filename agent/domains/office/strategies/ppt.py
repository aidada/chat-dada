from __future__ import annotations

from typing import Any


class PptStrategy:
    def build_plan(
        self,
        *,
        goal: str,
        requested_slide_count: int,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        slide_count = max(int(requested_slide_count or 0), 1)
        title = _infer_deck_title(goal, default_create_file)
        slide_titles = _build_reference_aligned_slide_titles(slide_count, merged_constraints)
        slides = [
            {
                "index": idx + 1,
                "title": slide_titles[idx],
                "role": _slide_role(idx + 1, slide_count),
                "section": _slide_section(idx + 1, slide_count),
                "takeaway": _slide_takeaway(slide_titles[idx], idx + 1, slide_count),
                "layout_type": _slide_layout_type(idx + 1, slide_count),
                "visual_requirements": _slide_visual_requirements(idx + 1, slide_count),
                "transition_required": idx + 1 > 1,
                "notes_required": idx + 1 not in {1, slide_count},
            }
            for idx in range(slide_count)
        ]
        batches = []
        batch_size = max(int(build_batch_size or 1), 1)
        for start in range(0, slide_count, batch_size):
            end = min(start + batch_size, slide_count)
            batch_slides = slides[start:end]
            batches.append(
                {
                    "index": len(batches),
                    "slide_start": start + 1,
                    "slide_end": end,
                    "slide_titles": [item["title"] for item in batch_slides],
                    "slide_roles": [item["role"] for item in batch_slides],
                    "section_names": [item["section"] for item in batch_slides],
                    "takeaways": [item["takeaway"] for item in batch_slides],
                    "layout_types": [item["layout_type"] for item in batch_slides],
                    "visual_requirements": [item["visual_requirements"] for item in batch_slides],
                    "objective": _batch_objective(batch_slides),
                }
            )
        return {
            "title": title,
            "slide_count": slide_count,
            "slides": slides,
            "batches": batches,
        }

    def summarize_plan(self, plan: dict[str, Any]) -> str:
        if not isinstance(plan, dict):
            return ""
        slides = list(plan.get("slides") or [])
        batches = list(plan.get("batches") or [])
        lines = [
            f"- deck_title: {str(plan.get('title', '') or '').strip()}",
            f"- planned_slide_count: {int(plan.get('slide_count', 0) or 0)}",
        ]
        if slides:
            lines.append("- slide_outline:")
            for slide in slides[:12]:
                lines.append(
                    f"  - slide[{int(slide.get('index', 0) or 0)}] {str(slide.get('title', '') or '').strip()} ({str(slide.get('role', '') or '').strip()}) :: takeaway={str(slide.get('takeaway', '') or '').strip()} :: layout={str(slide.get('layout_type', '') or '').strip()}"
                )
        if batches:
            lines.append("- build_batches:")
            for batch in batches:
                lines.append(
                    f"  - batch[{int(batch.get('index', 0) or 0)}] slides {int(batch.get('slide_start', 0) or 0)}-{int(batch.get('slide_end', 0) or 0)}: {', '.join(str(item) for item in batch.get('slide_titles', []) or [])} :: objective={str(batch.get('objective', '') or '').strip()}"
                )
        return "\n".join(lines)

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
        if not isinstance(plan, dict):
            fallback = self.build_plan(
                goal=goal,
                requested_slide_count=requested_slide_count,
                build_batch_size=build_batch_size,
                default_create_file=default_create_file,
                merged_constraints=merged_constraints,
            )
            return fallback, ["plan_not_dict"]

        issues: list[str] = []
        raw_slides = list(plan.get("slides") or []) if isinstance(plan.get("slides"), list) else []
        target_slide_count = int(plan.get("slide_count", 0) or 0) or int(requested_slide_count or 0) or len(raw_slides) or 1
        target_slide_count = max(target_slide_count, 1)
        fallback = self.build_plan(
            goal=goal,
            requested_slide_count=target_slide_count,
            build_batch_size=build_batch_size,
            default_create_file=default_create_file,
            merged_constraints=merged_constraints,
        )
        fallback_slides = list(fallback.get("slides") or [])

        title = str(plan.get("title", "") or "").strip()
        if not title:
            issues.append("missing_title")
            title = str(fallback.get("title", "") or "")

        normalized_slides: list[dict[str, Any]] = []
        required_keys = (
            "title",
            "role",
            "section",
            "takeaway",
            "layout_type",
            "visual_requirements",
            "transition_required",
            "notes_required",
        )
        for idx in range(target_slide_count):
            base_slide = dict(fallback_slides[idx] if idx < len(fallback_slides) else fallback_slides[-1])
            raw_slide = raw_slides[idx] if idx < len(raw_slides) and isinstance(raw_slides[idx], dict) else {}
            if not raw_slide:
                issues.append(f"missing_slide[{idx + 1}]")
            normalized = dict(base_slide)
            normalized["index"] = idx + 1
            for key in required_keys:
                value = raw_slide.get(key)
                if isinstance(value, str):
                    value = value.strip()
                if value not in ("", None):
                    normalized[key] = value
                elif key not in raw_slide:
                    issues.append(f"missing_slide[{idx + 1}].{key}")
            normalized["transition_required"] = bool(normalized.get("transition_required", idx + 1 > 1))
            normalized["notes_required"] = bool(normalized.get("notes_required", idx + 1 not in {1, target_slide_count}))
            normalized_slides.append(normalized)

        normalized_batches: list[dict[str, Any]] = []
        batch_size = max(int(build_batch_size or 1), 1)
        for start in range(0, target_slide_count, batch_size):
            batch_slides = normalized_slides[start:start + batch_size]
            normalized_batches.append(
                {
                    "index": len(normalized_batches),
                    "slide_start": start + 1,
                    "slide_end": start + len(batch_slides),
                    "slide_titles": [item["title"] for item in batch_slides],
                    "slide_roles": [item["role"] for item in batch_slides],
                    "section_names": [item["section"] for item in batch_slides],
                    "takeaways": [item["takeaway"] for item in batch_slides],
                    "layout_types": [item["layout_type"] for item in batch_slides],
                    "visual_requirements": [item["visual_requirements"] for item in batch_slides],
                    "objective": _batch_objective(batch_slides),
                }
            )

        if list(plan.get("batches") or []) != normalized_batches:
            issues.append("batch_plan_normalized")

        normalized_plan = {
            "title": title,
            "slide_count": target_slide_count,
            "slides": normalized_slides,
            "batches": normalized_batches,
        }
        return normalized_plan, list(dict.fromkeys(issues))

    def get_current_batch(self, plan: dict[str, Any], batch_index: int) -> dict[str, Any] | None:
        batches = list(plan.get("batches") or []) if isinstance(plan, dict) else []
        if batch_index < 0 or batch_index >= len(batches):
            return None
        return dict(batches[batch_index])

    def build_phase_guidance(
        self,
        *,
        plan: dict[str, Any],
        current_batch_index: int,
        repair_mode: bool,
        qa_feedback: str,
    ) -> str:
        batches = list(plan.get("batches") or [])
        total_batches = len(batches)
        batch = self.get_current_batch(plan, current_batch_index)
        if repair_mode:
            guidance = [
                "- 当前阶段: repair run",
                "- 本轮只允许针对上一轮 QA 问题做有限修复，不要重新规划整套 deck。",
                "- 修复完成后，必须执行 validate、view stats、view annotated，并返回更新后的 stats。",
            ]
            if qa_feedback:
                guidance.extend(["- 必须优先修复这些问题：", qa_feedback])
            return "\n".join(guidance)

        if batch is None:
            return "- 当前阶段: build\n- 所有 batch 已执行完，本轮如果需要继续操作，只允许执行最终 QA。"

        is_last_batch = current_batch_index >= total_batches - 1
        guidance = [
            "- 当前阶段: build",
            f"- 当前 batch: {current_batch_index + 1}/{max(total_batches, 1)}",
            f"- 只处理 slide {int(batch.get('slide_start', 0) or 0)}-{int(batch.get('slide_end', 0) or 0)}。",
            f"- 本批 slide 主题: {', '.join(str(item) for item in batch.get('slide_titles', []) or [])}",
        ]
        if is_last_batch:
            guidance.append("- 当前是最后一个 batch。完成本批写入后，执行 validate、view stats、view annotated，并在最终 JSON 中返回 stats。")
        else:
            guidance.append("- 当前不是最后一个 batch。完成本批写入即可，final validation deferred；返回结构化 JSON 时 validated=false。")
        return "\n".join(guidance)

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
        if requested_slide_count is not None:
            sections.append(f"- requested_slide_count: {requested_slide_count}")
        sections.append(f"- build_batch_size: {build_batch_size}")
        if plan:
            sections.extend(["- deck_plan:", self.summarize_plan(plan)])
        sections.append(f"- current_batch_index: {current_batch_index}")
        sections.append(f"- repair_mode: {str(repair_mode).lower()}")
        batch = self.get_current_batch(plan, current_batch_index)
        if batch is not None:
            sections.append(
                f"- current_batch_slide_range: {int(batch.get('slide_start', 0) or 0)}-{int(batch.get('slide_end', 0) or 0)}"
            )
            slide_titles = ", ".join(str(item) for item in batch.get("slide_titles", []) or [])
            if slide_titles:
                sections.append(f"- current_batch_slide_titles: {slide_titles}")
            takeaways = " | ".join(str(item) for item in batch.get("takeaways", []) or [])
            if takeaways:
                sections.append(f"- current_batch_takeaways: {takeaways}")
            layouts = ", ".join(str(item) for item in batch.get("layout_types", []) or [])
            if layouts:
                sections.append(f"- current_batch_layouts: {layouts}")
            objective = str(batch.get("objective", "") or "").strip()
            if objective:
                sections.append(f"- current_batch_objective: {objective}")
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
        if operation not in {"create", "transform"}:
            return []

        required_int_fields = (
            "slide_count",
            "content_slide_count",
            "notes_slide_count",
            "transition_slide_count",
            "visual_slide_count",
            "text_only_slide_count",
            "layout_variety_count",
            "picture_count",
            "chart_count",
            "table_count",
        )
        issues: list[dict[str, Any]] = []
        if not isinstance(stats, dict) or not stats:
            return [{"severity": "error", "message": "PPT 创建结果缺少质量 stats"}]

        normalized: dict[str, int] = {}
        missing_fields: list[str] = []
        for key in required_int_fields:
            value = stats.get(key)
            if isinstance(value, bool) or value is None:
                missing_fields.append(key)
                continue
            try:
                normalized[key] = int(value)
            except (TypeError, ValueError):
                missing_fields.append(key)
        if missing_fields:
            issues.append(
                {
                    "severity": "error",
                    "message": f"PPT 质量 stats 缺少或非法字段: {', '.join(missing_fields)}",
                }
            )
            return issues

        qa_checks = stats.get("qa_checks")
        qa_values = {str(item).strip() for item in qa_checks} if isinstance(qa_checks, list) else set()
        required_checks = {"view_stats", "view_annotated", "validate"}
        if not required_checks.issubset(qa_values):
            issues.append(
                {
                    "severity": "error",
                    "message": "PPT QA 未完整执行：必须包含 view_stats、view_annotated、validate",
                }
            )

        slide_count = normalized["slide_count"]
        content_slide_count = normalized["content_slide_count"]
        notes_slide_count = normalized["notes_slide_count"]
        transition_slide_count = normalized["transition_slide_count"]
        visual_slide_count = normalized["visual_slide_count"]
        text_only_slide_count = normalized["text_only_slide_count"]
        layout_variety_count = normalized["layout_variety_count"]

        if slide_count <= 0:
            issues.append({"severity": "error", "message": "PPT slide_count 必须大于 0"})
            return issues
        if content_slide_count < 0 or content_slide_count > slide_count:
            issues.append({"severity": "error", "message": "PPT content_slide_count 不合法"})
        if notes_slide_count < content_slide_count:
            issues.append({"severity": "error", "message": "并非所有内容 slide 都有 speaker notes"})
        if slide_count > 1 and transition_slide_count < slide_count - 1:
            issues.append({"severity": "error", "message": "PPT 第 2 张及之后的 slide 缺少 transition"})
        if visual_slide_count < max(1, content_slide_count):
            issues.append({"severity": "error", "message": "PPT 视觉密度不足：内容 slide 缺少非文字视觉元素"})
        if text_only_slide_count > 0:
            issues.append({"severity": "error", "message": "PPT 仍存在 text-only slides"})
        if slide_count >= 3 and layout_variety_count < min(3, slide_count):
            issues.append({"severity": "error", "message": "PPT 布局变化不足，缺少版式多样性"})

        return issues

    def advance_after_build(
        self,
        *,
        plan: dict[str, Any],
        current_batch_index: int,
        repair_mode: bool,
        completed_pages: int,
    ) -> dict[str, Any]:
        next_batch_index = current_batch_index
        next_completed_pages = completed_pages
        if repair_mode:
            return {
                "current_batch_index": next_batch_index,
                "completed_pages": next_completed_pages,
                "next_stage": "qa_fix",
            }

        batch = self.get_current_batch(plan, current_batch_index)
        if batch is not None:
            next_batch_index = current_batch_index + 1
            next_completed_pages = max(next_completed_pages, int(batch.get("slide_end", 0) or 0))
        next_stage = "build" if next_batch_index < len(plan.get("batches", []) or []) else "qa_fix"
        return {
            "current_batch_index": next_batch_index,
            "completed_pages": next_completed_pages,
            "next_stage": next_stage,
        }


_PPT_STRATEGY = PptStrategy()


def build_deck_plan(
    *,
    goal: str,
    requested_slide_count: int,
    build_batch_size: int,
    default_create_file: str,
    merged_constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _PPT_STRATEGY.build_plan(
        goal=goal,
        requested_slide_count=requested_slide_count,
        build_batch_size=build_batch_size,
        default_create_file=default_create_file,
        merged_constraints=merged_constraints,
    )


def summarize_deck_plan(plan: dict[str, Any]) -> str:
    return _PPT_STRATEGY.summarize_plan(plan)


def get_current_batch(plan: dict[str, Any], batch_index: int) -> dict[str, Any] | None:
    return _PPT_STRATEGY.get_current_batch(plan, batch_index)


def build_phase_guidance(
    *,
    deck_plan: dict[str, Any],
    current_batch_index: int,
    repair_mode: bool,
    qa_feedback: str,
) -> str:
    return _PPT_STRATEGY.build_phase_guidance(
        plan=deck_plan,
        current_batch_index=current_batch_index,
        repair_mode=repair_mode,
        qa_feedback=qa_feedback,
    )


def evaluate_quality_stats(
    *,
    operation: str,
    stats: dict[str, Any],
) -> list[dict[str, Any]]:
    return _PPT_STRATEGY.evaluate_quality_stats(operation=operation, stats=stats)


def advance_after_build(
    *,
    plan: dict[str, Any],
    current_batch_index: int,
    repair_mode: bool,
    completed_pages: int,
) -> dict[str, Any]:
    return _PPT_STRATEGY.advance_after_build(
        plan=plan,
        current_batch_index=current_batch_index,
        repair_mode=repair_mode,
        completed_pages=completed_pages,
    )


def _infer_deck_title(goal: str, default_create_file: str) -> str:
    text = str(goal or "").strip()
    if not text:
        stem = str(default_create_file or "").rsplit(".", 1)[0].replace("-", " ").strip()
        return stem or "Presentation"
    compact = text.replace("\n", " ").strip()
    return compact[:80]


def _build_slide_titles(slide_count: int) -> list[str]:
    base = [
        "封面",
        "目录",
        "背景与趋势",
        "核心问题",
        "关键洞察",
        "能力框架",
        "行动策略",
        "实践案例",
        "落地建议",
        "总结与展望",
        "补充说明",
        "结束页",
    ]
    if slide_count <= len(base):
        return base[:slide_count]
    titles = list(base)
    for idx in range(len(base) + 1, slide_count + 1):
        titles.append(f"扩展内容 {idx - len(base)}")
    return titles


def _build_reference_aligned_slide_titles(
    slide_count: int,
    merged_constraints: dict[str, Any] | None,
) -> list[str]:
    reference_titles: list[str] = []
    structure_constraints = merged_constraints.get("reference_structure_constraints") if isinstance(merged_constraints, dict) else {}
    units = structure_constraints.get("units") if isinstance(structure_constraints, dict) else []
    if isinstance(units, list):
        for unit in units:
            if not isinstance(unit, dict):
                continue
            title = str(unit.get("name", "") or "").strip()
            if title:
                reference_titles.append(title)
            if len(reference_titles) >= slide_count:
                break

    if len(reference_titles) >= slide_count:
        return reference_titles[:slide_count]

    fallback_titles = _build_slide_titles(slide_count)
    return reference_titles + fallback_titles[len(reference_titles):]


def _slide_role(index: int, slide_count: int) -> str:
    if index == 1:
        return "cover"
    if index == 2 and slide_count >= 4:
        return "agenda"
    if index == slide_count:
        return "summary"
    return "content"


def _slide_section(index: int, slide_count: int) -> str:
    if index == 1:
        return "opening"
    if index == 2 and slide_count >= 4:
        return "opening"
    if index == slide_count:
        return "closing"
    midpoint = max(slide_count - 2, 1) / 2
    return "context" if index - 2 <= midpoint else "action"


def _slide_takeaway(title: str, index: int, slide_count: int) -> str:
    if index == 1:
        return "建立整套演示的主题与语气"
    if index == 2 and slide_count >= 4:
        return "快速建立全局结构与阅读预期"
    if index == slide_count:
        return "收束核心观点并给出下一步行动"
    mapping = {
        "背景与趋势": "说明为什么这个主题在当前环境下重要",
        "核心问题": "明确受众当前最关键的矛盾与挑战",
        "关键洞察": "提炼最值得记住的核心判断",
        "能力框架": "展示系统化能力模型和结构关系",
        "行动策略": "给出可执行的方法与步骤",
        "实践案例": "用真实场景增强说服力",
        "落地建议": "把抽象观点转成行动清单",
    }
    return mapping.get(title, f"围绕“{title}”输出一条可记忆的核心结论")


def _slide_layout_type(index: int, slide_count: int) -> str:
    if index == 1:
        return "hero-cover"
    if index == 2 and slide_count >= 4:
        return "agenda-list"
    if index == slide_count:
        return "closing-cards"
    layouts = ("two-column", "cards-grid", "timeline", "comparison", "big-number", "process-flow")
    return layouts[(index - 3) % len(layouts)]


def _slide_visual_requirements(index: int, slide_count: int) -> list[str]:
    if index == 1:
        return ["hero-shape-background", "accent-divider"]
    if index == 2 and slide_count >= 4:
        return ["section-number-markers", "agenda-layout"]
    if index == slide_count:
        return ["three-card-summary", "accent-background"]
    visuals = [
        ["card-group", "accent-shape"],
        ["timeline", "connector-lines"],
        ["comparison-columns", "icon-or-shape"],
        ["big-number", "supporting-cards"],
        ["process-flow", "connector-arrows"],
        ["case-study-cards", "accent-panels"],
    ]
    return visuals[(index - 3) % len(visuals)]


def _batch_objective(batch_slides: list[dict[str, Any]]) -> str:
    titles = [str(item.get("title", "") or "").strip() for item in batch_slides]
    return f"完成 {', '.join(title for title in titles if title)} 的结构、正文、视觉元素与 notes"
