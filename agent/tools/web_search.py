"""
Web Search Tool — single-call function wrapping Tavily search.
Extracted from search_agent for standalone use.
"""

import os

from core.logger import log_async

try:
    from langchain_community.tools.tavily_search import TavilySearchResults
    from langchain_tavily import TavilySearch

    HAS_TAVILY = True
except ImportError:
    HAS_TAVILY = False


@log_async("tool", "web_search")
async def run(input_data) -> dict:
    if isinstance(input_data, str):
        query = input_data
    elif isinstance(input_data, dict):
        query = input_data.get("query", str(input_data))
    else:
        query = str(input_data)

    if not HAS_TAVILY:
        return {"status": "ok", "result": f"(Tavily search tool unavailable, skipping search for '{query}')"}

    if not str(os.environ.get("TAVILY_API_KEY") or "").strip():
        return {"status": "ok", "result": f"(TAVILY_API_KEY not configured, skipping search for '{query}')"}

    try:
        search = TavilySearch(max_results=5)
        results = await search.ainvoke(query)
    except Exception as exc:
        return {"status": "ok", "result": f"(Tavily search unavailable: {exc}. Skipping search for '{query}')"}

    formatted = "\n\n".join(f"[{item['url']}]\n{item['content']}" for item in results)
    if not formatted:
        formatted = f"(Tavily returned no results for '{query}')"

    return {"status": "ok", "result": formatted}
