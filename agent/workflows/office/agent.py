"""Office domain legacy entry point."""
from __future__ import annotations

from typing import Any

from core.logger import log_async

from agent.workflows.office.orchestrated import OfficeDomainResult


@log_async("office", "run_office_domain")
async def run_office_domain(input_data: dict[str, Any]) -> OfficeDomainResult:
    from agent.workflows.office.orchestrated import run_office_domain_orchestrated

    return await run_office_domain_orchestrated(input_data)
