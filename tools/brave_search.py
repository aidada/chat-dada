"""
Brave Search Tool — single-call function wrapping Brave Search.
Useful for broad web discovery and finding candidate pages.
"""
import json
import os

from core.logger import log_async

try:
    from langchain_community.tools.brave_search.tool import BraveSearch
    HAS_BRAVE = True
except ImportError:
    HAS_BRAVE = False


@log_async("tool", "brave_search")
async def run(input_data) -> dict:
    if isinstance(input_data, str):
        query = input_data
    elif isinstance(input_data, dict):
        query = input_data.get("query", str(input_data))
    else:
        query = str(input_data)

    if not HAS_BRAVE:
        return {"status": "ok", "result": f"(Brave Search tool unavailable, skipping search for '{query}')"}

    if not os.environ.get("BRAVE_SEARCH_API_KEY"):
        return {"status": "ok", "result": f"(BRAVE_SEARCH_API_KEY not configured, skipping search for '{query}')"}

    search = BraveSearch.from_search_kwargs({"count": 5})
    raw_results = await search.ainvoke(query)

    try:
        results = json.loads(raw_results)
    except (json.JSONDecodeError, TypeError):
        results = []

    formatted = "\n\n".join(
        f"[{item.get('link', '')}]\n{item.get('title', '')}\n{item.get('snippet', '')}".strip()
        for item in results
    )
    if not formatted:
        formatted = f"(Brave Search returned no results for '{query}')"

    return {"status": "ok", "result": formatted}
