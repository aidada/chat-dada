"""PPT compatibility wrappers around shared OfficeCLI tools."""
from __future__ import annotations

from agent.domains.office.tools import (
    OfficeCliBatchInput,
    OfficeCliCommandInput,
    SUPPORTED_FORMATS,
    SUPPORTED_VERBS,
    officecli,
    officecli_batch,
    officecli_run,
)


def get_ppt_tools():
    """Return tools available to the PPT compatibility layer."""
    return [officecli, officecli_batch]


__all__ = [
    "OfficeCliBatchInput",
    "OfficeCliCommandInput",
    "SUPPORTED_FORMATS",
    "SUPPORTED_VERBS",
    "get_ppt_tools",
    "officecli",
    "officecli_batch",
    "officecli_run",
]
