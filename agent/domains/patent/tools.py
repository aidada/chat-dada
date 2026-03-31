from __future__ import annotations

from capabilities.toolkits.browser_toolkit import browser_navigate_task


def get_patent_tools():
    """Return tools available to patent domain subagents."""
    from domain_agents.research.tools import CORE_TOOLS

    return list(CORE_TOOLS)


async def browser_verify_patent_page(task_description: str, *, enabled: bool = False) -> str:
    if not enabled:
        return "browser verification disabled"
    return await browser_navigate_task(task_description, role="search")
