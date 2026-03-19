"""
Academic Search Tool — searches Semantic Scholar and arXiv for papers.
Uses free public APIs, no API key required.
"""
import asyncio
import urllib.parse

import httpx

from core.logger import log_async


@log_async("tool", "academic_search")
async def run(input_data) -> dict:
    """
    Search academic papers on Semantic Scholar and arXiv.

    Args:
        input_data: str (query) or dict with "query" key
    """
    if isinstance(input_data, str):
        query = input_data
    elif isinstance(input_data, dict):
        query = input_data.get("query", str(input_data))
    else:
        query = str(input_data)

    results = await asyncio.gather(
        _search_semantic_scholar(query),
        _search_arxiv(query),
        return_exceptions=True,
    )

    parts = []
    for r in results:
        if isinstance(r, Exception):
            parts.append(f"(Search error: {r})")
        else:
            parts.append(r)

    return {"status": "ok", "result": "\n\n".join(parts)}


async def _search_semantic_scholar(query: str, limit: int = 5) -> str:
    """Search Semantic Scholar API (free, no key needed)."""
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": limit,
        "fields": "title,authors,year,abstract,url,citationCount",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return f"(Semantic Scholar: HTTP {resp.status_code})"
        data = resp.json()

    papers = data.get("data", [])
    if not papers:
        return "(Semantic Scholar: no results)"

    lines = ["## Semantic Scholar Results"]
    for p in papers:
        authors = ", ".join(a.get("name", "") for a in p.get("authors", [])[:3])
        lines.append(f"- **{p.get('title', 'N/A')}** ({p.get('year', 'N/A')})")
        lines.append(f"  Authors: {authors}")
        lines.append(f"  Citations: {p.get('citationCount', 0)}")
        if p.get("abstract"):
            lines.append(f"  Abstract: {p['abstract'][:200]}...")
        if p.get("url"):
            lines.append(f"  URL: {p['url']}")
    return "\n".join(lines)


async def _search_arxiv(query: str, limit: int = 5) -> str:
    """Search arXiv API (free, no key needed)."""
    encoded = urllib.parse.quote(query)
    url = f"http://export.arxiv.org/api/query?search_query=all:{encoded}&max_results={limit}&sortBy=relevance"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return f"(arXiv: HTTP {resp.status_code})"

    # Simple XML parsing (avoid heavy dependency)
    text = resp.text
    entries = text.split("<entry>")[1:]  # Skip header
    if not entries:
        return "(arXiv: no results)"

    lines = ["## arXiv Results"]
    for entry in entries[:limit]:
        title = _extract_tag(entry, "title").replace("\n", " ").strip()
        summary = _extract_tag(entry, "summary").replace("\n", " ").strip()[:200]
        arxiv_id = _extract_tag(entry, "id")
        lines.append(f"- **{title}**")
        lines.append(f"  {summary}...")
        lines.append(f"  URL: {arxiv_id}")
    return "\n".join(lines)


def _extract_tag(xml: str, tag: str) -> str:
    """Extract text between XML tags (simple, no namespace)."""
    start = xml.find(f"<{tag}>")
    if start == -1:
        start = xml.find(f"<{tag} ")
        if start == -1:
            return ""
        start = xml.find(">", start) + 1
    else:
        start += len(f"<{tag}>")
    end = xml.find(f"</{tag}>", start)
    if end == -1:
        return ""
    return xml[start:end]
