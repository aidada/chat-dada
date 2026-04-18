from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.domains.office.strategies.default import DefaultOfficeStrategy

_WRITE_OPERATIONS = {"create", "edit", "transform"}


class DocxStrategy(DefaultOfficeStrategy):
    def build_plan(
        self,
        *,
        goal: str,
        requested_slide_count: int,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        title = _infer_document_title(goal, default_create_file)
        sections = _build_sections(goal=goal, merged_constraints=merged_constraints)
        batches = _build_batches(sections=sections, build_batch_size=build_batch_size)
        section_count = len(sections)
        return {
            "title": title,
            "section_count": section_count,
            "sections": sections,
            "batches": batches,
            # Keep shared workflow fields stable until DOCX-native naming is threaded through it.
            "slide_count": section_count,
            "slides": sections,
        }

    def summarize_plan(self, plan: dict[str, Any]) -> str:
        if not isinstance(plan, dict):
            return ""
        sections = list(plan.get("sections") or [])
        batches = list(plan.get("batches") or [])
        lines = [
            f"- document_title: {str(plan.get('title', '') or '').strip()}",
            f"- planned_section_count: {int(plan.get('section_count', 0) or 0)}",
        ]
        if sections:
            lines.append("- section_outline:")
            for index, section in enumerate(sections[:12], start=1):
                lines.append(
                    f"  - section[{index}] {str(section.get('heading', '') or '').strip()} ({str(section.get('content_mode', '') or '').strip()}) :: {str(section.get('purpose', '') or '').strip()}"
                )
        if batches:
            lines.append("- build_batches:")
            for batch in batches:
                lines.append(
                    f"  - batch[{int(batch.get('index', 0) or 0)}] sections {int(batch.get('section_start', 0) or 0)}-{int(batch.get('section_end', 0) or 0)}: {', '.join(str(item) for item in batch.get('section_headings', []) or [])}"
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
        title = str(plan.get("title", "") or "").strip()
        if not title:
            issues.append("missing_title")

        raw_sections = plan.get("sections")
        if not isinstance(raw_sections, list) or not raw_sections:
            issues.append("missing_sections")
            fallback = self.build_plan(
                goal=goal,
                requested_slide_count=requested_slide_count,
                build_batch_size=build_batch_size,
                default_create_file=default_create_file,
                merged_constraints=merged_constraints,
            )
            if title:
                fallback["title"] = title
            return fallback, issues

        normalized_sections: list[dict[str, Any]] = []
        seen_headings: set[str] = set()
        section_shape_invalid = False
        for index, section in enumerate(raw_sections, start=1):
            if not isinstance(section, dict):
                issues.append("invalid_section_entry")
                section_shape_invalid = True
                continue
            heading = str(section.get("heading", "") or "").strip()
            if not heading:
                issues.append("missing_section_heading")
                section_shape_invalid = True
                continue
            heading_key = heading.casefold()
            if heading_key in seen_headings:
                issues.append("duplicate_section_heading")
                section_shape_invalid = True
                continue
            seen_headings.add(heading_key)
            normalized_sections.append(
                {
                    "index": index,
                    "heading": heading,
                    "purpose": str(section.get("purpose", "") or "").strip() or f"说明{heading}相关内容",
                    "key_points": _coerce_string_list(section.get("key_points"), fallback=[heading]),
                    "content_mode": _normalize_content_mode(section.get("content_mode")),
                    "style_requirements": _coerce_style_requirements(
                        section.get("style_requirements"),
                        fallback=_base_style_requirements(merged_constraints),
                    ),
                }
            )

        if section_shape_invalid or not normalized_sections:
            fallback = self.build_plan(
                goal=goal,
                requested_slide_count=requested_slide_count,
                build_batch_size=build_batch_size,
                default_create_file=default_create_file,
                merged_constraints=merged_constraints,
            )
            if title:
                fallback["title"] = title
            return fallback, issues

        raw_batches = plan.get("batches")
        normalized_batches = _build_batches(sections=normalized_sections, build_batch_size=build_batch_size)
        if not isinstance(raw_batches, list) or not raw_batches:
            issues.append("missing_batches")

        normalized_title = title or _infer_document_title(goal, default_create_file)
        normalized_plan = {
            "title": normalized_title,
            "section_count": len(normalized_sections),
            "sections": normalized_sections,
            "batches": normalized_batches,
            "slide_count": len(normalized_sections),
            "slides": normalized_sections,
        }
        return normalized_plan, issues

    def evaluate_quality_stats(
        self,
        *,
        operation: str,
        stats: dict[str, Any],
        plan: dict[str, Any] | None = None,
        merged_constraints: dict[str, Any] | None = None,
        result_meta: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if operation not in _WRITE_OPERATIONS:
            return []
        if not isinstance(stats, dict) or not stats:
            return [{"severity": "error", "message": "DOCX 写入结果缺少质量 stats"}]
        issues: list[dict[str, Any]] = []
        if _coerce_positive_int(stats.get("section_count")) is None:
            issues.append({"severity": "error", "message": "DOCX 质量 stats 缺少有效的 section_count"})

        protected_units = _coerce_string_list(
            dict((merged_constraints or {}).get("existing_document_profile") or {}).get("protected_units"),
            fallback=[],
        )
        if not protected_units:
            return issues

        protected_units_preserved = _coerce_optional_bool(
            stats.get("protected_units_preserved")
            if "protected_units_preserved" in stats
            else (result_meta or {}).get("protected_units_preserved")
        )
        if protected_units_preserved is False:
            issues.append({"severity": "error", "message": "DOCX protected sections 未保留"})
            return issues

        observed_headings = _coerce_string_list(stats.get("section_headings"), fallback=[])
        if not observed_headings:
            observed_headings = _coerce_string_list((result_meta or {}).get("section_headings"), fallback=[])

        if observed_headings:
            observed_keys = {heading.casefold() for heading in observed_headings}
            missing_units = [unit for unit in protected_units if unit.casefold() not in observed_keys]
            if missing_units:
                issues.append(
                    {
                        "severity": "error",
                        "message": f"DOCX protected sections 缺失或被修改: {', '.join(missing_units)}",
                    }
                )
        elif protected_units_preserved is not True:
            issues.append(
                {
                    "severity": "error",
                    "message": "DOCX 缺少 protected sections 保留证明；请返回 protected_units_preserved 或 section_headings",
                }
            )

        return issues

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
        sections = super().build_input_sections(
            goal=goal,
            operation=operation,
            format_hint=format_hint,
            runtime_target=runtime_target,
            default_create_file=default_create_file,
            requested_slide_count=requested_slide_count,
            build_batch_size=build_batch_size,
            source_files=source_files,
            context=context,
            qa_feedback=qa_feedback,
            plan=plan,
            current_batch_index=current_batch_index,
            repair_mode=repair_mode,
            merged_constraints=merged_constraints,
        )
        if operation != "edit":
            return sections

        goal_constraints = dict((merged_constraints or {}).get("goal_constraints") or {})
        target_sections = [
            str(item or "").strip()
            for item in goal_constraints.get("section_headings", [])
            if str(item or "").strip()
        ]
        if target_sections:
            sections.append(f"- target_sections: {', '.join(target_sections)}")

        existing_document_profile = dict((merged_constraints or {}).get("existing_document_profile") or {})
        protected_units = [
            str(item or "").strip()
            for item in existing_document_profile.get("protected_units", [])
            if str(item or "").strip()
        ]
        if protected_units:
            sections.append(f"- protected_sections: {', '.join(protected_units)}")

        return sections


def _infer_document_title(goal: str, default_create_file: str) -> str:
    text = str(goal or "").strip()
    if text:
        return text.replace("\n", " ").strip()[:80]
    stem = Path(str(default_create_file or "")).stem.replace("-", " ").strip()
    return stem or "Document"


def _build_sections(*, goal: str, merged_constraints: dict[str, Any] | None) -> list[dict[str, Any]]:
    headings = _collect_headings(merged_constraints=merged_constraints)
    if not headings:
        fallback_heading = str(goal or "").strip()[:24] or "正文"
        headings = [fallback_heading]

    style_requirements = _base_style_requirements(merged_constraints)
    sections: list[dict[str, Any]] = []
    for index, heading in enumerate(headings, start=1):
        sections.append(
            {
                "index": index,
                "heading": heading,
                "purpose": f"说明{heading}相关内容",
                "key_points": [heading],
                "content_mode": "mixed",
                "style_requirements": dict(style_requirements),
            }
        )
    return sections


def _collect_headings(*, merged_constraints: dict[str, Any] | None) -> list[str]:
    headings: list[str] = []
    seen: set[str] = set()

    goal_constraints = dict((merged_constraints or {}).get("goal_constraints") or {})
    section_headings = goal_constraints.get("section_headings")
    if isinstance(section_headings, list):
        for item in section_headings:
            heading = str(item or "").strip()
            if not heading:
                continue
            key = heading.casefold()
            if key in seen:
                continue
            seen.add(key)
            headings.append(heading)

    return headings


def _base_style_requirements(merged_constraints: dict[str, Any] | None) -> dict[str, Any]:
    style_constraints = dict((merged_constraints or {}).get("reference_style_constraints") or {})
    style_tokens = style_constraints.get("style_tokens")
    requirements = dict(style_tokens) if isinstance(style_tokens, dict) else {}
    goal_constraints = dict((merged_constraints or {}).get("goal_constraints") or {})
    formatting_instructions = goal_constraints.get("formatting_instructions")
    if isinstance(formatting_instructions, list):
        normalized = [str(item or "").strip() for item in formatting_instructions if str(item or "").strip()]
        if normalized:
            requirements["formatting_instructions"] = normalized
    return requirements


def _build_batches(*, sections: list[dict[str, Any]], build_batch_size: int) -> list[dict[str, Any]]:
    if not sections:
        return []
    batch_size = max(int(build_batch_size or 0), 1)
    batches: list[dict[str, Any]] = []
    for batch_index, start in enumerate(range(0, len(sections), batch_size), start=1):
        chunk = sections[start:start + batch_size]
        batches.append(
            {
                "index": batch_index,
                "section_start": start + 1,
                "section_end": start + len(chunk),
                "section_headings": [str(section.get("heading", "") or "") for section in chunk],
                # Keep generic aliases available for shared build flow prompts.
                "slide_start": start + 1,
                "slide_end": start + len(chunk),
                "slide_titles": [str(section.get("heading", "") or "") for section in chunk],
                "slide_roles": ["section" for _ in chunk],
            }
        )
    return batches


def _coerce_string_list(value: Any, *, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(fallback)
    normalized = [str(item or "").strip() for item in value if str(item or "").strip()]
    return normalized or list(fallback)


def _coerce_positive_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _coerce_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _normalize_content_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"text", "mixed", "list"}:
        return mode
    return "mixed"


def _coerce_style_requirements(value: Any, *, fallback: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return dict(fallback)
    return dict(value)


__all__ = ["DocxStrategy"]
