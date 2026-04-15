"""
PPT Domain Agent — legacy entry point.

DEPRECATED: Use agent.domains.ppt.orchestrated.run_ppt_domain_orchestrated instead.
This module is kept for backward compatibility. It delegates to the orchestrated version.
"""

from __future__ import annotations

import logging
from typing import Any

from core.logger import log_async

# Re-export for backward compatibility
from agent.domains.ppt.orchestrated import PptDomainResult

from agent.platform.emit import safe_emit_progress

_log = logging.getLogger("chatdada.ppt")

PPT_KEYWORDS = (
    "ppt",
    "PPT",
    "幻灯片",
    "演示文稿",
    "slide",
    "slides",
    "powerpoint",
    "presentation",
    "deck",
)

@log_async("ppt", "run_ppt_domain")
async def run_ppt_domain(input_data: dict[str, Any]) -> PptDomainResult:
    """Domain runner for PPT generation tasks. Delegates to orchestrated version."""
    from agent.domains.ppt.orchestrated import run_ppt_domain_orchestrated

    return await run_ppt_domain_orchestrated(input_data)
