"""
Dependency Graph Scheduler — executes steps respecting depends_on / parallel_with.
Groups steps into waves: steps with no unresolved dependencies run concurrently.
"""
import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Awaitable

from core.content_utils import extract_result_text
from core.logger import log_async
from core.registry import resolve_fn

log = logging.getLogger("chatdada.orchestrator")


@log_async("orchestrator", "execute_plan")
async def execute_plan(
    steps: list[dict],
    context: dict[str, Any],
    on_step: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """
    Execute a plan's steps respecting dependency order.

    Args:
        steps: List of step dicts with id, type, name, input_key, depends_on
        context: Shared context dict (step results stored as context[step_id])
        on_step: Optional progress callback

    Returns:
        Updated context dict with all step results.
    """
    completed: set[int] = set()
    step_map = {s["id"]: s for s in steps}
    all_ids = set(step_map.keys())

    while completed != all_ids:
        # Find ready steps: all dependencies satisfied
        ready = []
        for sid, step in step_map.items():
            if sid in completed:
                continue
            deps = set(step.get("depends_on", []))
            if deps.issubset(completed):
                ready.append(step)

        if not ready:
            raise RuntimeError(
                f"Deadlock: no steps ready. Completed={completed}, "
                f"Remaining={all_ids - completed}"
            )

        # Execute ready steps concurrently
        if on_step:
            names = ", ".join(s["name"] for s in ready)
            await on_step(f"Executing: {names}")

        tasks = [_run_step(step, context, step_map, on_step) for step in ready]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for step, result in zip(ready, results):
            sid = step["id"]
            if isinstance(result, Exception):
                if on_step:
                    await on_step(f"⚠️ Step {step['name']} failed: {result}")
                context[f"step_{sid}_error"] = str(result)
            else:
                context[f"step_{sid}"] = result
            completed.add(sid)

    return context


@log_async("orchestrator", "_run_step")
async def _run_step(
    step: dict,
    context: dict[str, Any],
    step_map: dict[int, dict[str, Any]],
    on_step: Callable[[str], Awaitable[None]] | None,
) -> Any:
    """Run a single step by resolving its capability and calling it."""
    name = step["name"]
    cap_type = step["type"]
    dependency_error = _find_dependency_error(step, context, step_map)
    if dependency_error:
        raise RuntimeError(f"Blocked by failed dependency: {dependency_error}")

    input_data = _resolve_step_input(step, context, step_map)

    if on_step:
        emoji = {"agent": "🤖", "tool": "🔧", "renderer": "📄"}.get(cap_type, "▶️")
        await on_step(f"{emoji} {name}: starting...")

    fn = resolve_fn(name)
    result = await fn(input_data) if asyncio.iscoroutinefunction(fn) else fn(input_data)
    _raise_on_error_result(name, result)

    if on_step:
        preview = str(result)[:100]
        await on_step(f"✅ {name}: done ({preview}...)")

    return result


def _resolve_step_input(
    step: dict[str, Any],
    context: dict[str, Any],
    step_map: dict[int, dict[str, Any]],
) -> Any:
    """Build the concrete input payload for a step."""
    if step["name"] == "writer":
        return _build_writer_input(step, context, step_map)
    if step["type"] == "renderer":
        return _build_renderer_input(step, context, step_map)

    input_key = step.get("input_key", "")
    if not input_key:
        return {}
    return context.get(input_key, {})


def _build_writer_input(
    step: dict[str, Any],
    context: dict[str, Any],
    step_map: dict[int, dict[str, Any]],
) -> dict[str, str]:
    base_input = context.get(step.get("input_key", ""), {})
    writer_input = dict(base_input) if isinstance(base_input, dict) else {}

    storyline = str(
        writer_input.get("storyline")
        or context.get("storyline")
        or "背景介绍\n核心发现\n分析讨论\n结论建议"
    )
    search_findings = _coerce_text(writer_input.get("search_findings"))
    doc_analysis = _coerce_text(writer_input.get("doc_analysis"))
    author = _coerce_text(writer_input.get("author") or context.get("author"))

    for dep_id in step.get("depends_on", []):
        dep_step = step_map.get(dep_id, {})
        dep_name = dep_step.get("name", "")
        dep_text = _extract_text_payload(context.get(f"step_{dep_id}"))
        if not dep_text:
            continue

        if dep_name in {"search", "deep_research"}:
            search_findings = _merge_sections(search_findings, dep_text)
        elif dep_name in {"doc_analyst", "data_analyst"}:
            doc_analysis = _merge_sections(doc_analysis, dep_text)

    return {
        "storyline": storyline,
        "search_findings": search_findings,
        "doc_analysis": doc_analysis,
        "author": author,
    }


def _build_renderer_input(
    step: dict[str, Any],
    context: dict[str, Any],
    step_map: dict[int, dict[str, Any]],
) -> Any:
    if step["name"] == "word_render":
        return _build_text_render_input(step, context, step_map, extension="docx")
    if step["name"] == "markdown_render":
        return _build_text_render_input(step, context, step_map, extension="md")

    input_key = step.get("input_key", "")
    if not input_key:
        return {}
    return context.get(input_key, {})


def _build_word_render_input(
    step: dict[str, Any],
    context: dict[str, Any],
    step_map: dict[int, dict[str, Any]],
) -> dict[str, str]:
    return _build_text_render_input(step, context, step_map, extension="docx")


def _build_text_render_input(
    step: dict[str, Any],
    context: dict[str, Any],
    step_map: dict[int, dict[str, Any]],
    *,
    extension: str,
) -> dict[str, str]:
    base_input = context.get(step.get("input_key", ""), {})
    render_input = dict(base_input) if isinstance(base_input, dict) else {}
    direct_content = _coerce_text(base_input) if isinstance(base_input, str) else ""

    title = _coerce_text(render_input.get("title") or context.get("title") or context.get("task") or "Report")
    output_path = _coerce_text(render_input.get("output_path")) or _default_output_path(title, extension)
    content = _coerce_text(render_input.get("content") or render_input.get("text") or direct_content)

    if not content:
        main_sections: list[str] = []
        appendix_sections: list[str] = []

        for dep_id in step.get("depends_on", []):
            dep_step = step_map.get(dep_id, {})
            dep_name = dep_step.get("name", "")
            dep_text = _extract_text_payload(context.get(f"step_{dep_id}")).strip()
            if not dep_text or _is_placeholder_analysis(dep_text):
                continue

            if dep_name in {"deep_research", "search", "translator", "summarizer", "data_analyst"}:
                main_sections.append(dep_text)
            elif dep_name == "doc_analyst":
                appendix_sections.append(f"## 附件分析\n{dep_text}")
            else:
                appendix_sections.append(dep_text)

        content = "\n\n".join(main_sections + appendix_sections).strip()

    return {
        "title": title,
        "content": content,
        "output_path": output_path,
    }


def _extract_text_payload(result: Any) -> str:
    return extract_result_text(result)


def _merge_sections(existing: str, incoming: str) -> str:
    parts = [part.strip() for part in (existing, incoming) if part and part.strip()]
    return "\n\n".join(parts)


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _is_placeholder_analysis(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return True

    placeholder_markers = (
        "请先提供需要分析的文件",
        "请提供需要分析的文件",
        "请上传需要分析的文件",
    )
    return any(marker in normalized for marker in placeholder_markers)


def _default_output_path(title: str, extension: str) -> str:
    safe_title = "".join(ch for ch in title if ch.isalnum() or ch in " _-").strip()
    stem = safe_title[:40] or Path(f"report.{extension}").stem
    return f"outputs/{stem}_{uuid.uuid4().hex[:8]}.{extension}"


def _find_dependency_error(
    step: dict[str, Any],
    context: dict[str, Any],
    step_map: dict[int, dict[str, Any]],
) -> str:
    for dep_id in step.get("depends_on", []):
        error = _coerce_text(context.get(f"step_{dep_id}_error")).strip()
        if not error:
            continue
        dep_name = step_map.get(dep_id, {}).get("name", f"step_{dep_id}")
        return f"{dep_name}: {error}"
    return ""


def _raise_on_error_result(name: str, result: Any) -> None:
    if not isinstance(result, dict):
        return

    status = str(result.get("status", "")).strip().lower()
    if status != "error":
        return

    message = _coerce_text(result.get("result") or result.get("error") or f"{name} returned an error")
    raise RuntimeError(message)
