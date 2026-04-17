from __future__ import annotations

from typing import Annotated, Any

from typing_extensions import TypedDict

from agent.domains.office.reference_models import (
    ConflictResolution,
    ExistingDocumentProfile,
    FidelityDeviation,
    GoalConstraints,
    ReferenceStyleConstraints,
    ReferenceStructureConstraints,
)


class OfficeWorkflowState(TypedDict, total=False):
    goal: str
    task_id: str
    report_profile: str
    format_hint: str
    file_hint: str
    default_create_file: str
    requested_slide_count: int
    build_batch_size: int
    source_files: list[str]
    reference_files: list[str]
    operation_hint: str
    quality_profile: dict[str, Any]
    cost_ledger: dict[str, Any]
    current_stage: str
    task_profile: dict[str, Any]
    goal_constraints: GoalConstraints
    reference_structure_constraints: ReferenceStructureConstraints
    reference_style_constraints: ReferenceStyleConstraints
    existing_document_profile: ExistingDocumentProfile
    conflict_resolution: ConflictResolution
    fidelity_deviations: list[FidelityDeviation]
    deck_plan: dict[str, Any]
    planning_summary: dict[str, Any]
    planner_validation_issues: list[str]
    current_batch_index: int
    completed_pages: int
    qa_fix_round: int
    max_qa_fix_rounds: int
    repair_mode: bool
    quality_report: dict[str, Any]
    partial_progress: dict[str, Any]

    format: str
    operation: str
    allowed_source_files: list[str]
    write_required: bool
    runtime_target_hint: str

    selected_strategy: str
    step_history: Annotated[list[dict[str, Any]], "add"]

    progress: float
    confidence: float
    coverage: dict[str, bool]
    cost: float
    max_cost: float
    max_steps: int
    inner_recursion_limit: int

    intermediate_results: Annotated[list[dict[str, Any]], "add"]
    evaluations: Annotated[list[dict[str, Any]], "add"]
    final_result: str
    terminal_status: str
    terminal_reason: str
