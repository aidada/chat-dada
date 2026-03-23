from __future__ import annotations

from capabilities.toolkits.browser_toolkit import browser_navigate_task


def get_zero_report_tools():
    """Return tools available to zero-report domain subagents."""
    from domain_agents.research.tools import CORE_TOOLS

    return list(CORE_TOOLS)


async def browser_collect_zero_report_context(task_description: str, *, enabled: bool = False) -> str:
    if not enabled:
        return "browser collection disabled"
    return await browser_navigate_task(task_description, role="search")
