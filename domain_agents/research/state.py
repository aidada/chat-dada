from __future__ import annotations

from typing import Any, Literal

from typing_extensions import TypedDict


ModuleStatus = Literal[
    "pending",
    "running",
    "completed",
    "locked",
    "needs_revision",
    "skipped",
]


class ResearchWorkflowState(TypedDict, total=False):
    query: str
    task_id: str
    report_profile: str
    input_payload: dict[str, Any]

    brief: dict[str, Any]
    plan: dict[str, Any]
    module_status: dict[str, ModuleStatus]
    module_outputs: dict[str, dict[str, Any]]
    evidence_bank: list[dict[str, Any]]
    citation_bank: list[dict[str, Any]]

    aggregated_draft: str
    draft_history: list[dict[str, Any]]
    evaluations: list[dict[str, Any]]
    revision_targets: list[dict[str, Any]]
    locked_modules: dict[str, str]

    feedback_history: list[dict[str, Any]]
    active_checkpoint: str
    pending_feedback_action: str
    needs_clarification: bool
    needs_replan: bool
    revision_round: int
    workflow_trace: list[str]

    progress: float
    cost: float
    final_result: str
