"""科研领域工具集合。"""
from __future__ import annotations

import os

from langchain_core.tools import tool

from agent.runtime.interaction import ask_user
from agent.capabilities.toolkits.browser_toolkit import browser_navigate_task
from agent.tools.research_notes import save_research_note, recall_research_notes
from agent.tools.brave_search import run as run_brave_search

try:
    from langchain_community.tools.tavily_search import TavilySearchResults
    HAS_TAVILY = True
except ImportError:
    HAS_TAVILY = False


def _configured_env_value(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _is_tavily_enabled() -> bool:
    return HAS_TAVILY and bool(_configured_env_value("TAVILY_API_KEY"))


@tool
async def web_search(query: str) -> str:
    """用 Tavily 搜索互联网，适合研究型查询和提取较完整的摘要。"""
    if not HAS_TAVILY:
        return f"(Tavily search tool unavailable, skipping '{query}')"
    if not _configured_env_value("TAVILY_API_KEY"):
        return f"(TAVILY_API_KEY not configured, skipping '{query}')"
    try:
        search = TavilySearchResults(max_results=5)
        results = await search.ainvoke(query)
    except Exception as exc:
        return f"(Tavily search unavailable: {exc}. Skipping '{query}')"
    return "\n\n".join(f"[{r['url']}]\n{r['content']}" for r in results)


@tool
async def academic_search(query: str) -> str:
    """搜索学术论文元信息，适合补齐作者、题目、引用线索和交叉验证。"""
    from agent.tools.academic_search import run as search_academic
    result = await search_academic({"query": query})
    return result.get("result", "No results")


@tool
async def brave_search(query: str) -> str:
    """用 Brave Search 快速发现候选网页和来源。"""
    result = await run_brave_search({"query": query})
    return result.get("result", "No results")


@tool
async def exa_deep_search(
    query: str,
    mode: str = "summary",
    output_format: str = "",
    num_results: int = 8,
    category: str = "research paper",
    summary_query: str = "",
    text_max_characters: int = 20000,
    text_verbosity: str = "standard",
    structured_schema_json: str = "",
) -> str:
    """用 Exa 做科研检索。

    用法建议：
    - 默认 `mode="summary"`：返回高密度摘要和 highlights，适合首轮广搜与证据摸底。
    - `mode="full_text"`：抓取正文并返回结构化结果，适合核查实验细节、方法步骤、claim 边界。
    - `structured_schema_json`：传 JSON Schema，让 Exa 返回结构化摘要。
    """
    from agent.tools.exa_search import run as search_exa

    result = await search_exa(
        {
            "query": query,
            "type": "deep",
            "category": category,
            "num_results": num_results,
            "result_mode": mode,
            "output_format": output_format,
            "summary_query": summary_query or query,
            "text_max_characters": text_max_characters,
            "text_verbosity": text_verbosity,
            "summary_schema": structured_schema_json,
            "highlights_max_characters": 1800 if mode == "summary" else 1200,
        }
    )
    return result.get("result", "No results")


@tool
async def browser_navigate(task_description: str) -> str:
    """控制浏览器完成复杂网页任务。"""
    return await browser_navigate_task(task_description)


@tool
async def ask_user_clarification(
    question: str,
    why: str = "",
    placeholder: str = "",
) -> str:
    """向用户确认研究方向或判断标准。仅当歧义会显著影响检索方向时调用，且最多调用一次。"""
    answer = await ask_user(question, context=why, placeholder=placeholder)
    if answer is None:
        return "用户交互当前不可用，请基于现有任务描述继续研究。"
    return answer


CORE_TOOLS = [
    exa_deep_search,
    academic_search,
    web_search,
    brave_search,
    browser_navigate,
    ask_user_clarification,
    save_research_note,
    recall_research_notes,
]


def get_research_tools():
    return [
        item
        for item in CORE_TOOLS
        if getattr(item, "name", "") != "web_search" or _is_tavily_enabled()
    ]
