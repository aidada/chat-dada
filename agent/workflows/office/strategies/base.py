from __future__ import annotations

from typing import Any, Protocol


class OfficeFormatStrategy(Protocol):
    def build_plan(
        self,
        *,
        goal: str,
        requested_slide_count: int,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def summarize_plan(self, plan: dict[str, Any]) -> str: ...

    def validate_plan(
        self,
        *,
        plan: dict[str, Any],
        goal: str,
        requested_slide_count: int,
        build_batch_size: int,
        default_create_file: str,
        merged_constraints: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[str]]: ...

    def build_phase_guidance(
        self,
        *,
        plan: dict[str, Any],
        current_batch_index: int,
        repair_mode: bool,
        qa_feedback: str,
    ) -> str: ...

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
    ) -> list[str]: ...

    def evaluate_quality_stats(
        self,
        *,
        operation: str,
        stats: dict[str, Any],
        plan: dict[str, Any] | None = None,
        merged_constraints: dict[str, Any] | None = None,
        result_meta: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    def advance_after_build(
        self,
        *,
        plan: dict[str, Any],
        current_batch_index: int,
        repair_mode: bool,
        completed_pages: int,
    ) -> dict[str, Any]: ...
