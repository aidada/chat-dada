from __future__ import annotations

from agent.capabilities.toolkits.browser_toolkit import browser_navigate_task


def get_patent_tools():
    """Return tools available to patent domain subagents."""
    from agent.workflows.research.tools import get_research_tools

    return get_research_tools()


async def browser_verify_patent_page(task_description: str, *, enabled: bool = False) -> str:
    if not enabled:
        return "browser verification disabled"
    return await browser_navigate_task(task_description, role="search")
