"""
Academic Search Tool — searches Semantic Scholar and arXiv for papers.
Uses free public APIs, no API key required.
"""
from __future__ import annotations

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
    providers: list[dict[str, object]] = []
    total_results = 0
    error_count = 0
    for r in results:
        if isinstance(r, Exception):
            parts.append(f"(Search error: {r})")
            providers.append({"provider": "unknown", "status": "error", "result_count": 0, "message": str(r)})
            error_count += 1
        else:
            parts.append(r["text"])
            providers.append(
                {
                    "provider": r["provider"],
                    "status": r["status"],
                    "result_count": r["result_count"],
                    "message": r["message"],
                }
            )
            total_results += int(r["result_count"])
            if r["status"] == "error":
                error_count += 1

    status = "ok"
    if total_results <= 0:
        status = "no_results" if error_count == 0 else "degraded"
    elif error_count:
        status = "degraded"

    return {
        "status": status,
        "query": query,
        "result": "\n\n".join(part for part in parts if str(part).strip()),
        "providers": providers,
        "total_results": total_results,
        "fallback_hint": "exa_deep_search" if total_results <= 0 or error_count else "",
    }


async def _search_semantic_scholar(query: str, limit: int = 5) -> dict[str, object]:
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
            return {
                "provider": "semantic_scholar",
                "status": "error",
                "result_count": 0,
                "message": f"HTTP {resp.status_code}",
                "text": f"(Semantic Scholar: HTTP {resp.status_code})",
            }
        data = resp.json()

    papers = data.get("data", [])
    if not papers:
        return {
            "provider": "semantic_scholar",
            "status": "empty",
            "result_count": 0,
            "message": "no results",
            "text": "(Semantic Scholar: no results)",
        }

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
    return {
        "provider": "semantic_scholar",
        "status": "ok",
        "result_count": len(papers),
        "message": "",
        "text": "\n".join(lines),
    }


async def _search_arxiv(query: str, limit: int = 5) -> dict[str, object]:
    """Search arXiv API (free, no key needed)."""
    encoded = urllib.parse.quote(query)
    url = f"https://export.arxiv.org/api/query?search_query=all:{encoded}&max_results={limit}&sortBy=relevance"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return {
                "provider": "arxiv",
                "status": "error",
                "result_count": 0,
                "message": f"HTTP {resp.status_code}",
                "text": f"(arXiv: HTTP {resp.status_code})",
            }

    # Simple XML parsing (avoid heavy dependency)
    text = resp.text
    entries = text.split("<entry>")[1:]  # Skip header
    if not entries:
        return {
            "provider": "arxiv",
            "status": "empty",
            "result_count": 0,
            "message": "no results",
            "text": "(arXiv: no results)",
        }

    lines = ["## arXiv Results"]
    for entry in entries[:limit]:
        title = _extract_tag(entry, "title").replace("\n", " ").strip()
        summary = _extract_tag(entry, "summary").replace("\n", " ").strip()[:200]
        arxiv_id = _extract_tag(entry, "id")
        lines.append(f"- **{title}**")
        lines.append(f"  {summary}...")
        lines.append(f"  URL: {arxiv_id}")
    return {
        "provider": "arxiv",
        "status": "ok",
        "result_count": min(len(entries), limit),
        "message": "",
        "text": "\n".join(lines),
    }


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
