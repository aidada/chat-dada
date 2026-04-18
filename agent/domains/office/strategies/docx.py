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
    ) -> list[dict[str, Any]]:
        if operation not in _WRITE_OPERATIONS:
            return []
        if not isinstance(stats, dict) or not stats:
            return [{"severity": "error", "message": "DOCX 写入结果缺少质量 stats"}]
        return []


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
    hard_requirements = goal_constraints.get("hard_requirements")
    if isinstance(hard_requirements, list):
        for item in hard_requirements:
            heading = str(item or "").strip()
            if not heading or _is_instruction_like_heading(heading):
                continue
            key = heading.casefold()
            if key in seen:
                continue
            seen.add(key)
            headings.append(heading)

    return headings


_INSTRUCTION_LIKE_HEADING_TOKENS = (
    "preserve ",
    "keep ",
    "maintain ",
    "retain ",
    "ensure ",
    "use ",
    "follow ",
    "avoid ",
    "remove ",
    "update ",
    "change ",
    "rename ",
    "formatting",
    "format ",
    "layout",
    "style",
    "should ",
    "must ",
    "need ",
    "不要",
    "保持",
    "保留",
    "避免",
    "使用",
    "更新",
)


def _is_instruction_like_heading(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return True
    return any(token in normalized for token in _INSTRUCTION_LIKE_HEADING_TOKENS)


def _base_style_requirements(merged_constraints: dict[str, Any] | None) -> dict[str, Any]:
    style_constraints = dict((merged_constraints or {}).get("reference_style_constraints") or {})
    style_tokens = style_constraints.get("style_tokens")
    if isinstance(style_tokens, dict):
        return dict(style_tokens)
    return {}


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
