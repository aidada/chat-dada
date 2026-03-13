"""
Web Search Tool — single-call function wrapping Tavily search.
Extracted from search_agent for standalone use.
"""
try:
    from langchain_community.tools.tavily_search import TavilySearchResults
    HAS_TAVILY = True
except ImportError:
    HAS_TAVILY = False


async def run(input_data) -> dict:
    """
    Search the web for a query.

    Args:
        input_data: str (query) or dict with "query" key
    """
    if isinstance(input_data, str):
        query = input_data
    elif isinstance(input_data, dict):
        query = input_data.get("query", str(input_data))
    else:
        query = str(input_data)

    if not HAS_TAVILY:
        return {"status": "ok", "result": f"(TAVILY_API_KEY not configured, skipping search for '{query}')"}

    search = TavilySearchResults(max_results=5)
    results = await search.ainvoke(query)
    formatted = "\n\n".join(f"[{r['url']}]\n{r['content']}" for r in results)
    return {"status": "ok", "result": formatted}
