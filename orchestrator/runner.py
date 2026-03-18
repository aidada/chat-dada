"""
Orchestrator Runner — main entry point for all tasks.
Replaces agents/orchestrator.py with registry-driven execution.

Flow:
1. Planner classifies intent → picks template or generates plan
2. For ppt_report template: generate storyline first (backward compat)
3. Scheduler executes steps with dependency resolution
4. Returns final result to caller
"""
import json
import logging
import uuid
from pathlib import Path
from typing import Callable, Awaitable

from core.content_utils import extract_result_text
from langchain_core.messages import HumanMessage, SystemMessage

from core.logger import log_async
from storage.user_store_v2 import MemoryStoreV2
from core.models import get_llm, response_text
from orchestrator.planner import classify_and_plan

log = logging.getLogger("chatdada.orchestrator")


# Storyline generation prompt (for PPT tasks, backward compat with existing writer)
STORYLINE_SYSTEM = """你是一个任务编排 Agent。用户会给你一个研究或报告任务。

你需要输出一个 JSON，格式如下：
{
  "storyline": "PPT 大纲，用 \\n 分隔每个章节标题",
  "search_queries": ["搜索关键词1", "搜索关键词2", ...],
  "file_paths": ["如有本地文件路径写在这里"],
  "title": "PPT 标题",
  "author": "作者（如用户未提供则留空）"
}

注意：
- search_queries: 为搜索 Agent 提供 2-5 个搜索关键词
- file_paths: 从用户消息中提取文件路径（如有）
- storyline: 规划 PPT 的叙事结构，每行一个章节
- 只输出 JSON，不要其他内容"""


@log_async("orchestrator", "run_orchestrator")
async def run_orchestrator(
    task: str,
    on_step: Callable[[str], Awaitable[None]],
    user_id: str = "anonymous",
) -> str:
    """
    Main entry point — replaces agents.orchestrator.run_agent().
    Same callback interface for backward compatibility with main.py.
    """
    await on_step("🧠 Orchestrator: 分析任务...")

    memory_store = MemoryStoreV2()
    memory_context = ""
    try:
        memory_recall = await memory_store.recall_with_merge(user_id, task)
        memory_context = memory_recall.to_prompt()
        if memory_recall.has_content():
            await on_step(
                "🧠 Memory: 已召回 "
                f"{len(memory_recall.facts)} 条用户画像，"
                f"{len(memory_recall.active_projects)} 个活跃项目。"
            )
    except Exception as exc:
        await on_step(f"⚠️ Memory recall failed: {exc}")

    # Step 1: Classify and plan
    plan = await classify_and_plan(task, memory_context=memory_context)
    intent = plan["intent"]
    await on_step(f"📋 Intent: {intent}")

    # Step 2: Route to appropriate handler
    if intent == "ppt_report":
        result = await _handle_ppt_report(task, plan, on_step, memory_context=memory_context)
    elif intent == "quick_question":
        result = await _handle_quick_question(task, on_step, memory_context=memory_context)
    else:
        result = await _handle_generic(task, plan, on_step, memory_context=memory_context)

    try:
        await memory_store.remember(user_id, task, result, intent=intent)
    except Exception as exc:
        await on_step(f"⚠️ Memory save failed: {exc}")
    return result


@log_async("orchestrator", "_handle_ppt_report")
async def _handle_ppt_report(
    task: str,
    plan: dict,
    on_step: Callable,
    *,
    memory_context: str = "",
) -> str:
    """Handle PPT report generation — uses existing agents pipeline."""
    import asyncio
    from agents.search_agent import run_search
    from agents.doc_agent import run_doc_analysis
    from agents.writer_agent import run_writer
    from ppt_engine.renderer import render_pptx

    # Generate storyline
    await on_step("📝 Generating storyline...")
    llm = get_llm("orchestrator")
    messages = [
        SystemMessage(content=STORYLINE_SYSTEM),
        *([SystemMessage(content=memory_context)] if memory_context else []),
        HumanMessage(content=task),
    ]
    response = await llm.ainvoke(messages)

    try:
        content = _extract_json(response_text(response))
        storyline_plan = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        storyline_plan = {
            "storyline": "背景介绍\n核心内容\n数据分析\n总结展望",
            "search_queries": [task],
            "file_paths": [],
            "title": task[:30],
            "author": "",
        }

    storyline = storyline_plan.get("storyline", "")
    search_queries = storyline_plan.get("search_queries", [])
    file_paths = storyline_plan.get("file_paths", [])
    title = storyline_plan.get("title", "Report")
    author = storyline_plan.get("author", "")

    await on_step(f"📋 Storyline:\n{storyline}")

    # Parallel: Search + Doc
    tasks = []
    if search_queries:
        await on_step(f"🔍 Search Agent: searching {len(search_queries)} queries...")
        combined = "\n".join(f"- {q}" for q in search_queries)
        search_input = {"query": combined, "memory_context": memory_context} if memory_context else combined
        tasks.append(("search", run_search(search_input)))
    if file_paths:
        await on_step(f"📄 Doc Agent: analyzing {len(file_paths)} files...")
        tasks.append(("doc", run_doc_analysis(file_paths)))

    search_findings = ""
    doc_analysis = ""

    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
        for (label, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                await on_step(f"⚠️ {label} error: {result}")
                continue
            if label == "search":
                search_findings = result
                await on_step(f"🔍 Search done: {len(result)} chars")
            elif label == "doc":
                doc_analysis = result
                await on_step(f"📄 Doc done: {len(result)} chars")

    # Writer
    await on_step("✍️ Writer Agent: generating slides...")
    try:
        deck = await run_writer(storyline, search_findings, doc_analysis, author)
        deck.meta.title = title
        if author:
            deck.meta.author = author
        await on_step(f"✍️ Writer done: {len(deck.slides)} slides")
    except Exception as e:
        await on_step(f"⚠️ Writer failed: {e}")
        return f"PPT 内容生成失败: {e}"

    # Render
    await on_step("📊 Rendering .pptx...")
    file_id = uuid.uuid4().hex[:8]
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip()[:30] or "report"
    filename = f"{safe_title}_{file_id}.pptx"
    output_path = f"outputs/{filename}"

    try:
        render_pptx(deck, output_path)
        await on_step(f"✅ PPT generated: {filename}")
        await on_step(json.dumps({"type": "file", "url": f"/download/{filename}", "name": filename}))
    except Exception as e:
        await on_step(f"⚠️ Render failed: {e}")
        return f"PPT 渲染失败: {e}"

    return f"PPT 已生成：《{title}》，共 {len(deck.slides)} 页。\n下载: /download/{filename}"


@log_async("orchestrator", "_handle_quick_question")
async def _handle_quick_question(
    task: str,
    on_step: Callable,
    *,
    memory_context: str = "",
) -> str:
    """Handle direct Q&A — single LLM call, no tools."""
    from agents.general_chat import generate_reply

    await on_step("💬 Answering directly...")

    async def on_chunk(content: str) -> None:
        if not content:
            return
        await on_step(json.dumps({"type": "result_delta", "content": content}, ensure_ascii=False))

    return await generate_reply(task, memory_context=memory_context, on_chunk=on_chunk)


@log_async("orchestrator", "_handle_generic")
async def _handle_generic(
    task: str,
    plan: dict,
    on_step: Callable,
    *,
    memory_context: str = "",
) -> str:
    """Handle generic tasks — use scheduler for dependency-based execution."""
    from orchestrator.scheduler import execute_plan

    steps = plan.get("steps", [])
    context = _inject_memory_context(plan.get("context", {"task": task}), memory_context)

    if not steps:
        return await _handle_quick_question(task, on_step, memory_context=memory_context)

    await on_step(f"🚀 Executing {len(steps)} steps...")
    result_ctx = await execute_plan(steps, context, on_step)
    failure_message = _collect_plan_failure(steps, result_ctx)
    if failure_message:
        return failure_message

    await _emit_generated_files(steps, result_ctx, on_step)

    # Find the last step's result
    final_step = max(steps, key=lambda item: item["id"])
    final_id = final_step["id"]
    final = result_ctx.get(f"step_{final_id}", result_ctx.get(f"step_{final_id}_error", "任务完成。"))
    return _format_generic_result(final_step, final, steps, result_ctx, context)


def _extract_json(text: str) -> str:
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    return text.strip()


def _inject_memory_context(context: dict, memory_context: str) -> dict:
    if not memory_context:
        return context

    enriched = dict(context)
    enriched["memory_context"] = memory_context

    for key in ("chat_input", "search_query", "analysis_input"):
        value = enriched.get(key)
        if not value:
            continue
        if isinstance(value, dict):
            value.setdefault("memory_context", memory_context)
        elif isinstance(value, str):
            enriched[key] = {"query": value, key: value, "memory_context": memory_context}

    return enriched


async def _emit_generated_files(
    steps: list[dict],
    result_ctx: dict,
    on_step: Callable[[str], Awaitable[None]],
) -> None:
    emitted: set[str] = set()
    for step in sorted(steps, key=lambda item: item["id"]):
        result = result_ctx.get(f"step_{step['id']}")
        for file_path in _extract_result_files(result):
            filename = Path(file_path).name
            if not filename or filename in emitted:
                continue
            emitted.add(filename)
            await on_step(
                json.dumps(
                    {"type": "file", "url": f"/download/{filename}", "name": filename},
                    ensure_ascii=False,
                )
            )


def _format_generic_result(
    final_step: dict,
    final: object,
    steps: list[dict],
    result_ctx: dict,
    context: dict,
) -> str:
    if isinstance(final, str):
        return final

    result_text = _extract_result_text(final)
    if final_step.get("type") == "renderer":
        filenames = [Path(path).name for path in _extract_result_files(final)]
        title = str(context.get("title") or context.get("task") or "任务成果")
        summary = _build_dependency_summary(final_step, steps, result_ctx)
        file_line = (
            f"《{title}》已生成，下载见上方文件卡片。"
            if filenames
            else f"《{title}》已处理完成。"
        )
        if summary:
            return f"{file_line}\n\n内容摘要：\n{summary}"
        if result_text and not result_text.lower().startswith(
            ("word document saved:", "markdown file saved:", "excel file saved:", "visio file saved:")
        ):
            return f"{file_line}\n\n{result_text}"
        return file_line

    if result_text:
        return result_text
    return json.dumps(final, ensure_ascii=False)


def _extract_result_files(result: object) -> list[str]:
    if not isinstance(result, dict):
        return []

    files = result.get("files")
    if isinstance(files, list):
        return [str(item) for item in files if item]

    file_path = result.get("file") or result.get("output_path")
    return [str(file_path)] if file_path else []


def _extract_result_text(result: object) -> str:
    return extract_result_text(result)


def _build_dependency_summary(
    final_step: dict,
    steps: list[dict],
    result_ctx: dict,
) -> str:
    step_map = {step["id"]: step for step in steps}
    snippets: list[str] = []

    for dep_id in final_step.get("depends_on", []):
        dep_step = step_map.get(dep_id, {})
        dep_name = dep_step.get("name", "")
        dep_text = _extract_result_text(result_ctx.get(f"step_{dep_id}"))
        excerpt = _summarize_text(dep_text)
        if not excerpt:
            continue

        label = {
            "deep_research": "研究结论",
            "search": "搜索结果",
            "doc_analyst": "附件分析",
            "data_analyst": "数据分析",
            "translator": "译文摘要",
        }.get(dep_name)
        snippets.append(f"### {label}\n{excerpt}" if label else excerpt)

    return "\n\n".join(snippets[:2])


def _summarize_text(text: str, max_chars: int = 320) -> str:
    if not text:
        return ""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    selected: list[str] = []
    current_len = 0
    for line in lines:
        if len(selected) >= 6:
            break
        projected_len = current_len + len(line) + (1 if selected else 0)
        if projected_len > max_chars and selected:
            break
        selected.append(line)
        current_len = projected_len

    if selected:
        return "\n".join(selected)

    compact = " ".join(lines)
    return compact[: max_chars - 1].rstrip() + "…" if len(compact) > max_chars else compact


def _collect_plan_failure(steps: list[dict], result_ctx: dict) -> str:
    errors: list[str] = []
    for step in sorted(steps, key=lambda item: item["id"]):
        error = _extract_result_text(result_ctx.get(f"step_{step['id']}_error"))
        if not error:
            continue
        errors.append(f"{step['name']}: {error}")

    if not errors:
        return ""

    lines = ["任务未能完成，失败步骤如下："]
    for item in errors[:3]:
        lines.append(f"- {item}")
    return "\n".join(lines)
