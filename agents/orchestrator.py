"""
Orchestrator Agent — main graph that:
1. Understands the task and generates a storyline
2. Dispatches Search + Doc agents in parallel
3. Runs Writer to produce Slide DSL
4. Renders PPT via PPT Engine

Exposes run_agent() with the same callback interface as the old agent.py.
"""
import asyncio
import json
import uuid
from typing import Callable, Awaitable

from langchain_core.messages import HumanMessage, SystemMessage

from models import get_llm
from agents.search_agent import run_search
from agents.doc_agent import run_doc_analysis
from agents.writer_agent import run_writer
from ppt_engine.renderer import render_pptx


ORCHESTRATOR_SYSTEM = """你是一个任务编排 Agent。用户会给你一个研究或报告任务。

你需要输出一个 JSON 执行计划，格式如下：
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


async def run_agent(task: str, on_step: Callable[[str], Awaitable[None]]) -> str:
    """
    Main entry point — same interface as the old agent.py run_agent().
    Returns final result text. Sends file info via on_step callback.
    """
    await on_step("🧠 Orchestrator: 分析任务，规划执行计划...")

    # Step 1: Orchestrator plans the task
    llm = get_llm("orchestrator")
    messages = [
        SystemMessage(content=ORCHESTRATOR_SYSTEM),
        HumanMessage(content=task),
    ]
    response = await llm.ainvoke(messages)
    content = response.content

    # Parse plan JSON
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        content = content.split("```")[1].split("```")[0]
    content = content.strip()

    try:
        plan = json.loads(content)
    except json.JSONDecodeError:
        await on_step("⚠️ 执行计划解析失败，使用默认计划...")
        plan = {
            "storyline": f"背景介绍\n核心内容\n数据分析\n总结展望",
            "search_queries": [task],
            "file_paths": [],
            "title": task[:30],
            "author": "",
        }

    storyline = plan.get("storyline", "")
    search_queries = plan.get("search_queries", [])
    file_paths = plan.get("file_paths", [])
    title = plan.get("title", "Report")
    author = plan.get("author", "")

    await on_step(f"📋 Storyline:\n{storyline}")

    # Step 2: Dispatch Search + Doc agents in parallel
    tasks = []

    # Search tasks
    if search_queries:
        await on_step(f"🔍 Search Agent: 开始搜索 {len(search_queries)} 个主题...")
        combined_query = "\n".join(f"- {q}" for q in search_queries)
        tasks.append(("search", run_search(combined_query)))

    # Doc analysis tasks
    if file_paths:
        await on_step(f"📄 Doc Agent: 开始分析 {len(file_paths)} 个文件...")
        tasks.append(("doc", run_doc_analysis(file_paths)))

    # Run concurrently
    search_findings = ""
    doc_analysis = ""

    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
        for (label, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                await on_step(f"⚠️ {label} Agent 出错: {result}")
                continue
            if label == "search":
                search_findings = result
                await on_step(f"🔍 Search Agent: 搜索完成，收集到 {len(result)} 字素材")
            elif label == "doc":
                doc_analysis = result
                await on_step(f"📄 Doc Agent: 文档分析完成，提取 {len(result)} 字要点")
    else:
        await on_step("ℹ️ 无搜索/文档任务，直接进入内容生成...")

    # Step 3: Writer produces Slide DSL
    await on_step("✍️ Writer Agent: 正在生成 PPT 内容...")
    try:
        deck = await run_writer(storyline, search_findings, doc_analysis, author)
        # Override title from plan
        deck.meta.title = title
        if author:
            deck.meta.author = author
        await on_step(f"✍️ Writer Agent: 完成，共 {len(deck.slides)} 页 Slide")
    except Exception as e:
        await on_step(f"⚠️ Writer 生成失败: {e}")
        return f"PPT 内容生成失败: {e}"

    # Step 4: Render to .pptx
    await on_step("📊 PPT Engine: 正在渲染 .pptx 文件...")
    file_id = uuid.uuid4().hex[:8]
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip()[:30] or "report"
    filename = f"{safe_title}_{file_id}.pptx"
    output_path = f"outputs/{filename}"

    try:
        render_pptx(deck, output_path)
        await on_step(f"✅ PPT 已生成: {filename}")
        # Send file download message
        await on_step(json.dumps({
            "type": "file",
            "url": f"/download/{filename}",
            "name": filename,
        }))
    except Exception as e:
        await on_step(f"⚠️ PPT 渲染失败: {e}")
        return f"PPT 渲染失败: {e}"

    return f"PPT 已生成完成：《{title}》，共 {len(deck.slides)} 页。\n下载链接: /download/{filename}"
