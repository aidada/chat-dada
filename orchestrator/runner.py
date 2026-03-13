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
import uuid
from typing import Callable, Awaitable

from langchain_core.messages import HumanMessage, SystemMessage

from models import get_llm
from orchestrator.planner import classify_and_plan


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


async def run_orchestrator(task: str, on_step: Callable[[str], Awaitable[None]]) -> str:
    """
    Main entry point — replaces agents.orchestrator.run_agent().
    Same callback interface for backward compatibility with main.py.
    """
    await on_step("🧠 Orchestrator: 分析任务...")

    # Step 1: Classify and plan
    plan = await classify_and_plan(task)
    intent = plan["intent"]
    await on_step(f"📋 Intent: {intent}")

    # Step 2: Route to appropriate handler
    if intent == "ppt_report":
        return await _handle_ppt_report(task, plan, on_step)
    elif intent == "quick_question":
        return await _handle_quick_question(task, on_step)
    else:
        return await _handle_generic(task, plan, on_step)


async def _handle_ppt_report(task: str, plan: dict, on_step: Callable) -> str:
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
        HumanMessage(content=task),
    ]
    response = await llm.ainvoke(messages)

    try:
        content = _extract_json(response.content)
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
        tasks.append(("search", run_search(combined)))
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


async def _handle_quick_question(task: str, on_step: Callable) -> str:
    """Handle direct Q&A — single LLM call, no tools."""
    await on_step("💬 Answering directly...")
    llm = get_llm("orchestrator")
    messages = [
        SystemMessage(content="你是一个专业的AI助手。直接回答用户的问题，简洁准确。"),
        HumanMessage(content=task),
    ]
    response = await llm.ainvoke(messages)
    return response.content


async def _handle_generic(task: str, plan: dict, on_step: Callable) -> str:
    """Handle generic tasks — use scheduler for dependency-based execution."""
    from orchestrator.scheduler import execute_plan

    steps = plan.get("steps", [])
    context = plan.get("context", {"task": task})

    if not steps:
        return await _handle_quick_question(task, on_step)

    await on_step(f"🚀 Executing {len(steps)} steps...")
    result_ctx = await execute_plan(steps, context, on_step)

    # Find the last step's result
    max_id = max(s["id"] for s in steps)
    final = result_ctx.get(f"step_{max_id}", result_ctx.get(f"step_{max_id}_error", "任务完成。"))
    return str(final)


def _extract_json(text: str) -> str:
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    return text.strip()
