"""
Exa 搜索工具。

设计目标：
1. 默认返回适合科研工作流首轮检索的高密度摘要；
2. 在需要核查正文时，支持拉取全文并返回结构化结果；
3. 尽量直接贴合 Exa 官方推荐的 `deep` 搜索、`research paper` 类别、
   `highlights` / `summary` / `text` 内容选项。
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from core.logger import log_async

try:
    from exa_py import Exa

    HAS_EXA = True
except ImportError:
    HAS_EXA = False

VALID_TYPES = {"auto", "instant", "fast", "neural", "keyword", "deep", "deep-reasoning"}
VALID_CATEGORIES = {
    "company",
    "research paper",
    "news",
    "tweet",
    "personal site",
    "financial report",
    "people",
}
VALID_RESULT_MODES = {"summary", "highlights", "summary_and_highlights", "full_text", "full"}
VALID_OUTPUT_FORMATS = {"markdown", "json"}
VALID_TEXT_VERBOSITY = {"compact", "standard", "full"}


def _normalize_input(input_data: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(input_data, str):
        return input_data, {}
    if isinstance(input_data, dict):
        query = str(input_data.get("query", "") or str(input_data)).strip()
        return query, dict(input_data)
    return str(input_data), {}


def _normalize_result_mode(raw_mode: Any) -> str:
    mode = str(raw_mode or "summary").strip().lower()
    return mode if mode in VALID_RESULT_MODES else "summary"


def _normalize_output_format(raw_format: Any, *, result_mode: str, structured: bool) -> str:
    normalized = str(raw_format or "").strip().lower()
    if normalized in VALID_OUTPUT_FORMATS:
        return normalized
    if structured or result_mode in {"full_text", "full"}:
        return "json"
    return "markdown"


def _safe_int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(parsed, minimum)
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


def _safe_json_loads(value: Any) -> dict[str, Any] | list[Any] | None:
    if value in (None, "", {}):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _build_search_kwargs(query: str, params: dict[str, Any], *, result_mode: str) -> dict[str, Any]:
    search_kwargs: dict[str, Any] = {}

    search_type = str(params.get("type", "deep") or "deep").strip().lower()
    if search_type in VALID_TYPES:
        search_kwargs["type"] = search_type

    category = params.get("category")
    if category and category in VALID_CATEGORIES:
        search_kwargs["category"] = category

    search_kwargs["num_results"] = _safe_int(params.get("num_results"), 10, minimum=1, maximum=100)

    for key in (
        "include_domains",
        "exclude_domains",
        "start_published_date",
        "end_published_date",
        "start_crawl_date",
        "end_crawl_date",
        "include_text",
        "exclude_text",
        "additional_queries",
        "system_prompt",
        "user_location",
        "flags",
    ):
        if params.get(key):
            search_kwargs[key] = params[key]

    if params.get("output_schema") is not None:
        search_kwargs["output_schema"] = params["output_schema"]

    if params.get("contents") is not None:
        search_kwargs["contents"] = params["contents"]
        return search_kwargs

    summary_query = str(params.get("summary_query", "") or query).strip()
    summary_schema = _safe_json_loads(params.get("summary_schema"))
    livecrawl = params.get("livecrawl")
    max_age_hours = params.get("max_age_hours")

    contents: dict[str, Any] = {}
    if result_mode in {"summary", "summary_and_highlights", "full_text", "full"}:
        summary_opts: dict[str, Any] = {"query": summary_query}
        if summary_schema is not None:
            summary_opts["schema"] = summary_schema
        contents["summary"] = summary_opts

    if result_mode in {"highlights", "summary_and_highlights", "summary", "full_text", "full"}:
        contents["highlights"] = {
            "query": summary_query,
            "max_characters": _safe_int(params.get("highlights_max_characters"), 2000, minimum=200, maximum=12000),
        }

    if livecrawl is not None:
        contents["livecrawl"] = livecrawl
    if max_age_hours is not None:
        contents["max_age_hours"] = max_age_hours

    if contents:
        search_kwargs["contents"] = contents
    return search_kwargs


def _build_get_contents_kwargs(params: dict[str, Any], query: str) -> dict[str, Any]:
    summary_query = str(params.get("summary_query", "") or query).strip()
    summary_schema = _safe_json_loads(params.get("summary_schema"))
    verbosity = str(params.get("text_verbosity", "standard") or "standard").strip().lower()
    if verbosity not in VALID_TEXT_VERBOSITY:
        verbosity = "standard"

    text_opts: dict[str, Any] = {
        "max_characters": _safe_int(params.get("text_max_characters"), 20000, minimum=1000, maximum=200000),
        "verbosity": verbosity,
    }
    if params.get("include_html_tags") is not None:
        text_opts["include_html_tags"] = bool(params.get("include_html_tags"))
    if params.get("include_sections"):
        text_opts["include_sections"] = params["include_sections"]
    if params.get("exclude_sections"):
        text_opts["exclude_sections"] = params["exclude_sections"]

    kwargs: dict[str, Any] = {"text": text_opts}
    if summary_schema is not None or params.get("include_summary_with_text", True):
        summary_opts: dict[str, Any] = {"query": summary_query}
        if summary_schema is not None:
            summary_opts["schema"] = summary_schema
        kwargs["summary"] = summary_opts

    if params.get("livecrawl") is not None:
        kwargs["livecrawl"] = params["livecrawl"]
    if params.get("livecrawl_timeout") is not None:
        kwargs["livecrawl_timeout"] = _safe_int(params["livecrawl_timeout"], 10000, minimum=1000)

    max_age_hours = params.get("max_age_hours")
    if max_age_hours is not None:
        kwargs["max_age_hours"] = max_age_hours
    elif verbosity in {"standard", "full"}:
        # Exa 文档说明更高 verbosity 依赖 fresh crawl，因此默认切到 0。
        kwargs["max_age_hours"] = 0

    if params.get("filter_empty_results") is not None:
        kwargs["filter_empty_results"] = bool(params.get("filter_empty_results"))
    return kwargs


def _search_response_to_json(
    response: Any,
    *,
    result_mode: str,
    query: str,
    include_text: bool,
) -> str:
    payload: dict[str, Any] = {
        "query": query,
        "resolved_search_type": getattr(response, "resolved_search_type", None),
        "search_time": getattr(response, "search_time", None),
        "cost_dollars": getattr(getattr(response, "cost_dollars", None), "total", None),
        "result_mode": result_mode,
        "results": [],
    }

    output = getattr(response, "output", None)
    if output is not None:
        payload["deep_output"] = {
            "content": getattr(output, "content", None),
            "grounding": [
                {
                    "field": item.field,
                    "confidence": item.confidence,
                    "citations": [{"url": c.url, "title": c.title} for c in item.citations],
                }
                for item in getattr(output, "grounding", []) or []
            ],
        }

    for item in getattr(response, "results", []) or []:
        row = {
            "title": getattr(item, "title", None),
            "url": getattr(item, "url", None),
            "published_date": getattr(item, "published_date", None),
            "author": getattr(item, "author", None),
            "score": getattr(item, "score", None),
            "summary": getattr(item, "summary", None),
            "highlights": getattr(item, "highlights", None),
        }
        if include_text:
            row["text"] = getattr(item, "text", None)
        payload["results"].append(row)

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _has_meaningful_deep_output(response: Any) -> bool:
    output = getattr(response, "output", None)
    if output is None:
        return False

    content = getattr(output, "content", None)
    if isinstance(content, str) and content.strip():
        return True
    if isinstance(content, dict) and content:
        return True
    if isinstance(content, list) and content:
        return True

    grounding = getattr(output, "grounding", None)
    if isinstance(grounding, list) and grounding:
        return True
    return False


def _search_response_to_markdown(response: Any, *, include_text: bool) -> str:
    formatted_parts: list[str] = []
    for item in getattr(response, "results", []) or []:
        parts: list[str] = []
        url = getattr(item, "url", None)
        title = getattr(item, "title", None)
        published = getattr(item, "published_date", None)
        author = getattr(item, "author", None)
        summary = getattr(item, "summary", None)
        highlights = getattr(item, "highlights", None)
        score = getattr(item, "score", None)
        text = getattr(item, "text", None)

        if url:
            parts.append(f"[{url}]")
        if title:
            parts.append(f"**{title}**")
        meta: list[str] = []
        if author:
            meta.append(f"作者: {author}")
        if published:
            meta.append(f"发布时间: {published}")
        if score is not None:
            meta.append(f"分数: {score:.4f}")
        if meta:
            parts.append(" | ".join(meta))
        if summary:
            parts.append(f"摘要: {summary}")
        if highlights:
            parts.append("高亮:\n" + "\n".join(f"  - {highlight}" for highlight in highlights))
        if include_text and text:
            display = text[:5000] + "..." if len(text) > 5000 else text
            parts.append("全文:\n" + display)
        formatted_parts.append("\n".join(parts))

    return "\n\n---\n\n".join(formatted_parts)


@log_async("tool", "exa_search")
async def run(input_data) -> dict:
    """运行 Exa 搜索。

    常用参数：
    - `query`: 查询词
    - `type`: 搜索类型，默认 `deep`
    - `category`: 类别，例如 `research paper`
    - `result_mode`: `summary` 或 `full_text`
    - `output_format`: `markdown` 或 `json`
    - `summary_schema`: 用于结构化摘要的 JSON Schema
    """
    query, params = _normalize_input(input_data)

    if not HAS_EXA:
        return {"status": "ok", "result": f"(Exa search tool unavailable — exa_py not installed, skipping search for '{query}')"}

    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        return {"status": "ok", "result": f"(EXA_API_KEY not configured, skipping search for '{query}')"}

    result_mode = _normalize_result_mode(params.get("result_mode") or params.get("mode"))
    structured = bool(params.get("structured")) or params.get("summary_schema") is not None or params.get("output_schema") is not None
    output_format = _normalize_output_format(
        params.get("output_format"),
        result_mode=result_mode,
        structured=structured,
    )

    try:
        exa = Exa(api_key=api_key)
        loop = asyncio.get_running_loop()
        search_kwargs = _build_search_kwargs(query, params, result_mode=result_mode)
        response = await loop.run_in_executor(None, lambda: exa.search(query, **search_kwargs))

        if result_mode in {"full_text", "full"}:
            contents_kwargs = _build_get_contents_kwargs(params, query)
            response = await loop.run_in_executor(
                None,
                lambda: exa.get_contents(getattr(response, "results", []) or [], **contents_kwargs),
            )
    except Exception as exc:
        return {"status": "error", "result": f"Exa search error: {exc}"}

    results = getattr(response, "results", []) or []
    if not results and not _has_meaningful_deep_output(response):
        return {"status": "ok", "result": f"(Exa returned no results for '{query}')"}

    include_text = result_mode in {"full_text", "full"}
    if output_format == "json":
        result_text = _search_response_to_json(
            response,
            result_mode=result_mode,
            query=query,
            include_text=include_text,
        )
    else:
        result_text = _search_response_to_markdown(response, include_text=include_text)

    return {"status": "ok", "result": result_text}
