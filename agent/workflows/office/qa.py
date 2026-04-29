from __future__ import annotations

from agent.workflows.office.core.qa import run_qa_fix_stage
from agent.workflows.office.core.state import OfficeWorkflowState
from agent.workflows.office.strategies.base import OfficeFormatStrategy


def run_quality_gate(
    state: OfficeWorkflowState,
    *,
    strategy: OfficeFormatStrategy,
) -> dict:
    return run_qa_fix_stage(state, strategy=strategy)
