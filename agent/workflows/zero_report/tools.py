from __future__ import annotations

from agent.capabilities.toolkits.browser_toolkit import browser_navigate_task


def get_zero_report_tools():
    """Return tools available to zero-report domain subagents."""
    from agent.workflows.research.tools import get_research_tools

    return get_research_tools()


async def browser_collect_zero_report_context(task_description: str, *, enabled: bool = False) -> str:
    if not enabled:
        return "browser collection disabled"
    return await browser_navigate_task(task_description, role="search")
