from __future__ import annotations

from typing import Any

from agent.workflows.office.core.build import run_build_stage
from agent.workflows.office.core.state import OfficeWorkflowState
from agent.workflows.office.strategies.base import OfficeFormatStrategy


async def run_section_builder(
    state: OfficeWorkflowState,
    *,
    strategy: OfficeFormatStrategy,
    system_template: str,
    format_specific_guidance: str,
    office_model_role: str,
    subagents: list[dict[str, Any]],
) -> dict[str, Any]:
    return await run_build_stage(
        state,
        strategy=strategy,
        system_template=system_template,
        format_specific_guidance=format_specific_guidance,
        office_model_role=office_model_role,
        subagents=subagents,
    )
