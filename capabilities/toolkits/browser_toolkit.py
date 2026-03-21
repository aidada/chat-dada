from __future__ import annotations

from core.models import get_browser_use_llm


async def browser_navigate_task(task_description: str, *, role: str, llm=None) -> str:
    from browser_use import Agent as BrowserAgent
    from browser_use import BrowserSession as Browser
    from browser_use import BrowserProfile as BrowserConfig

    browser = Browser(browser_profile=BrowserConfig(headless=True))
    llm = llm or get_browser_use_llm(role)
    agent = BrowserAgent(task=task_description, llm=llm, browser=browser, max_actions_per_step=5)
    result = await agent.run(max_steps=10)
    final = result.final_result() if hasattr(result, "final_result") else str(result)
    return final or "Browser task done."
