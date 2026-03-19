"""
Exa Deep Search Tool — AI-powered semantic search with deep research capability.
Supports deep/neural/keyword search with text extraction, highlights, and summaries.
"""
import asyncio
import os

from core.logger import log_async

try:
    from exa_py import Exa
    HAS_EXA = True
except ImportError:
    HAS_EXA = False

VALID_TYPES = {"auto", "instant", "fast", "neural", "keyword", "deep", "deep-reasoning"}
VALID_CATEGORIES = {
    "company", "research paper", "news", "tweet",
    "personal site", "financial report", "people",
}


@log_async("tool", "exa_search")
async def run(input_data) -> dict:
    """Run an Exa search. Accepts string query or dict with advanced params."""
    # --- parse input ---
    if isinstance(input_data, str):
        query = input_data
        params = {}
    elif isinstance(input_data, dict):
        query = input_data.get("query", str(input_data))
        params = input_data
    else:
        query = str(input_data)
        params = {}

    if not HAS_EXA:
        return {"status": "ok", "result": f"(Exa search tool unavailable — exa_py not installed, skipping search for '{query}')"}

    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        return {"status": "ok", "result": f"(EXA_API_KEY not configured, skipping search for '{query}')"}

    # --- build search kwargs ---
    search_kwargs = {}

    search_type = params.get("type", "deep")
    if search_type in VALID_TYPES:
        search_kwargs["type"] = search_type

    category = params.get("category")
    if category and category in VALID_CATEGORIES:
        search_kwargs["category"] = category

    num_results = params.get("num_results", 10)
    search_kwargs["num_results"] = min(int(num_results), 100)

    if params.get("include_domains"):
        search_kwargs["include_domains"] = params["include_domains"]
    if params.get("exclude_domains"):
        search_kwargs["exclude_domains"] = params["exclude_domains"]
    if params.get("start_published_date"):
        search_kwargs["start_published_date"] = params["start_published_date"]
    if params.get("end_published_date"):
        search_kwargs["end_published_date"] = params["end_published_date"]

    contents = params.get("contents")
    if contents is None:
        contents = {"text": {"max_characters": 4000}}
    search_kwargs["contents"] = contents

    # --- execute search (sync SDK → run_in_executor) ---
    try:
        exa = Exa(api_key=api_key)
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, lambda: exa.search(query, **search_kwargs)
        )
    except Exception as e:
        return {"status": "error", "result": f"Exa search error: {e}"}

    # --- format results ---
    results = getattr(response, "results", [])
    if not results:
        return {"status": "ok", "result": f"(Exa returned no results for '{query}')"}

    formatted_parts = []
    for r in results:
        parts = []
        url = getattr(r, "url", None)
        title = getattr(r, "title", None)
        published = getattr(r, "published_date", None)
        author = getattr(r, "author", None)
        text = getattr(r, "text", None)
        highlights = getattr(r, "highlights", None)
        summary = getattr(r, "summary", None)
        score = getattr(r, "score", None)

        if url:
            parts.append(f"[{url}]")
        if title:
            parts.append(f"**{title}**")
        meta = []
        if author:
            meta.append(f"Author: {author}")
        if published:
            meta.append(f"Published: {published}")
        if score is not None:
            meta.append(f"Score: {score:.4f}")
        if meta:
            parts.append(" | ".join(meta))
        if summary:
            parts.append(f"Summary: {summary}")
        if highlights:
            parts.append("Highlights:\n" + "\n".join(f"  - {h}" for h in highlights))
        if text:
            display = text[:3000] + "..." if len(text) > 3000 else text
            parts.append(display)

        formatted_parts.append("\n".join(parts))

    return {"status": "ok", "result": "\n\n---\n\n".join(formatted_parts)}
