"""
Research tools — shared tool definitions used by research graphs, patent, and zero-report domains.
"""
from __future__ import annotations

from langchain_core.tools import tool

from capabilities.toolkits.browser_toolkit import browser_navigate_task
from core.models import get_browser_use_llm
from runtime.task_interaction import ask_user
from tools.research_notes import save_research_note, recall_research_notes

try:
    from langchain_community.tools.tavily_search import TavilySearchResults
    HAS_TAVILY = True
except ImportError:
    HAS_TAVILY = False


@tool
async def web_search(query: str) -> str:
    """用 Tavily 搜索互联网，适合研究型查询和提取较完整的摘要。"""
    if HAS_TAVILY:
        search = TavilySearchResults(max_results=5)
        results = await search.ainvoke(query)
        return "\n\n".join(f"[{r['url']}]\n{r['content']}" for r in results)
    return f"(TAVILY_API_KEY not configured, skipping '{query}')"


@tool
async def academic_search(query: str) -> str:
    """搜索学术论文（Semantic Scholar + arXiv）。"""
    from tools.academic_search import run as search_academic
    result = await search_academic({"query": query})
    return result.get("result", "No results")


@tool
async def exa_deep_search(query: str) -> str:
    """用 Exa 进行深度语义搜索，适合查找学术论文、研究报告和深度分析文章。支持 deep search 模式，返回全文摘要。"""
    from tools.exa_search import run as search_exa
    result = await search_exa({
        "query": query,
        "type": "deep",
        "category": "research paper",
        "num_results": 10,
        "contents": {
            "text": {"max_characters": 4000},
            "highlights": {"query": query, "max_characters": 2000},
        },
    })
    return result.get("result", "No results")


@tool
async def browser_navigate(task_description: str) -> str:
    """控制浏览器完成复杂网页任务。"""
    llm = get_browser_use_llm("deep_research")
    return await browser_navigate_task(task_description, role="deep_research", llm=llm)


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


CORE_TOOLS = [academic_search, exa_deep_search, browser_navigate,
              ask_user_clarification, save_research_note, recall_research_notes]


def get_research_tools():
    return list(CORE_TOOLS)
