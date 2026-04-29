from __future__ import annotations

import asyncio
import re
from typing import Any

from core.models import get_browser_use_llm

BROWSER_USE_MODEL_ROLE = "browser_agent"
BROWSER_USE_MAX_STEPS = 6
BROWSER_USE_MAX_FAILURES = 2
BROWSER_USE_LLM_TIMEOUT_SECONDS = 45
BROWSER_USE_STEP_TIMEOUT_SECONDS = 90
BROWSER_USE_RUN_TIMEOUT_SECONDS = 240

_URL_RE = re.compile(r"https?://[^\s，。；;、）)】\]<>\"']+")
_URL_WITH_DELIMITER_RE = re.compile(r"(https?://[^\s，。；;、）)】\]<>\"']+)[，。；;、,]*")


def _clean_browser_url(url: str) -> str:
    return url.rstrip("，,。；;、:：）)】]'\"")


def _normalize_browser_task(task_description: str) -> tuple[str, str | None]:
    text = str(task_description or "").strip()
    match = _URL_RE.search(text)
    clean_url = _clean_browser_url(match.group(0)) if match else None
    if clean_url:
        text = _URL_WITH_DELIMITER_RE.sub(lambda match: f"{_clean_browser_url(match.group(1))} ", text)
        text = " ".join(text.split())
    return text, clean_url


def _browser_error_result(task_description: str, *, reason: str, error: BaseException | None = None) -> str:
    _, clean_url = _normalize_browser_task(task_description)
    detail = f": {error}" if error else ""
    url_hint = f" URL={clean_url}." if clean_url else ""
    return f"Browser task {reason}{detail}.{url_hint} Continue with non-browser sources or direct page data."


async def browser_navigate_task(
    task_description: str,
    *,
    role: str = BROWSER_USE_MODEL_ROLE,
    llm=None,
) -> str:
    from browser_use import Agent as BrowserAgent
    from browser_use import BrowserSession as Browser
    from browser_use import BrowserProfile as BrowserConfig

    normalized_task, clean_url = _normalize_browser_task(task_description)
    initial_actions: list[dict[str, dict[str, Any]]] | None = None
    if clean_url:
        initial_actions = [{"navigate": {"url": clean_url, "new_tab": False}}]

    browser = Browser(browser_profile=BrowserConfig(headless=True))
    llm = llm or get_browser_use_llm(role)
    agent = BrowserAgent(
        task=normalized_task,
        llm=llm,
        browser=browser,
        initial_actions=initial_actions,
        max_actions_per_step=5,
        max_failures=BROWSER_USE_MAX_FAILURES,
        llm_timeout=BROWSER_USE_LLM_TIMEOUT_SECONDS,
        step_timeout=BROWSER_USE_STEP_TIMEOUT_SECONDS,
        use_judge=False,
    )
    try:
        result = await asyncio.wait_for(
            agent.run(max_steps=BROWSER_USE_MAX_STEPS),
            timeout=BROWSER_USE_RUN_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        return _browser_error_result(task_description, reason="timed out", error=exc)
    except Exception as exc:
        return _browser_error_result(task_description, reason=f"failed ({type(exc).__name__})", error=exc)
    final = result.final_result() if hasattr(result, "final_result") else str(result)
    return final or "Browser task done."
