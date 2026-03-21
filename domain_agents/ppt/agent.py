"""
PPT Domain Agent — generates slide decks via outline → search → doc analysis → write → render.

Migrated from orchestrator/runner.py to align with the domain agent pattern.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from core.logger import log_async
from core.models import get_llm, response_text
from storage.user_store_v2 import MemoryStoreV2

_log = logging.getLogger("chatdada.ppt")

PPT_KEYWORDS = (
    "ppt", "PPT", "幻灯片", "演示文稿", "slide", "slides", "powerpoint",
    "presentation", "deck",
)

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


class PptDomainResult(BaseModel):
    status: str
    result: str
    artifact_refs: list[dict[str, Any]]
    review: dict[str, Any]
    budget: dict[str, Any]


def _safe_emit(event_type: str, content: str) -> None:
    """Emit a progress event via LangGraph stream writer, silently no-op outside a graph."""
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        writer({"event_type": event_type, "content": content})
    except Exception:
        pass


def _extract_json(text: str) -> str:
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    return text.strip()


@log_async("ppt", "run_ppt_domain")
async def run_ppt_domain(input_data: dict[str, Any]) -> PptDomainResult:
    """Domain runner for PPT generation tasks."""
    from domain_agents.ppt.search_agent import run_search
    from domain_agents.ppt.doc_agent import run_doc_analysis
    from domain_agents.ppt.writer_agent import run_writer
    from ppt_engine.renderer import render_pptx

    query = input_data.get("query") or input_data.get("task", "")
    task_id = input_data.get("task_id", "ppt_unknown")

    _safe_emit("step", "🧠 分析 PPT 任务...")

    # Memory recall
    memory_context = ""
    try:
        memory_store = MemoryStoreV2()
        user_id = input_data.get("user_id", "anonymous")
        memory_recall = await memory_store.recall_with_merge(user_id, query)
        memory_context = memory_recall.to_prompt()
        if memory_recall.has_content():
            _safe_emit(
                "step",
                f"🧠 Memory: 已召回 {len(memory_recall.facts)} 条用户画像，"
                f"{len(memory_recall.active_projects)} 个活跃项目。",
            )
    except Exception as exc:
        _safe_emit("step", f"⚠️ Memory recall failed: {exc}")

    # Generate storyline
    _safe_emit("step", "📝 生成大纲...")
    llm = get_llm("orchestrator")
    messages = [
        SystemMessage(content=STORYLINE_SYSTEM),
        *([SystemMessage(content=memory_context)] if memory_context else []),
        HumanMessage(content=query),
    ]
    response = await llm.ainvoke(messages)

    try:
        content = _extract_json(response_text(response))
        storyline_plan = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        storyline_plan = {
            "storyline": "背景介绍\n核心内容\n数据分析\n总结展望",
            "search_queries": [query],
            "file_paths": [],
            "title": query[:30],
            "author": "",
        }

    storyline = storyline_plan.get("storyline", "")
    search_queries = storyline_plan.get("search_queries", [])
    file_paths = storyline_plan.get("file_paths", [])
    title = storyline_plan.get("title", "Report")
    author = storyline_plan.get("author", "")

    _safe_emit("step", f"📋 Storyline:\n{storyline}")

    # Parallel: Search + Doc analysis
    tasks = []
    if search_queries:
        _safe_emit("step", f"🔍 搜索: 正在检索 {len(search_queries)} 个关键词...")
        combined = "\n".join(f"- {q}" for q in search_queries)
        search_input = {"query": combined, "memory_context": memory_context} if memory_context else combined
        tasks.append(("search", run_search(search_input)))
    if file_paths:
        _safe_emit("step", f"📄 文档分析: 正在处理 {len(file_paths)} 个文件...")
        tasks.append(("doc", run_doc_analysis(file_paths)))

    search_findings = ""
    doc_analysis = ""

    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
        for (label, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                _safe_emit("step", f"⚠️ {label} error: {result}")
                continue
            if label == "search":
                search_findings = result
                _safe_emit("step", f"🔍 搜索完成: {len(result)} 字")
            elif label == "doc":
                doc_analysis = result
                _safe_emit("step", f"📄 文档分析完成: {len(result)} 字")

    # Writer
    _safe_emit("step", "✍️ 正在撰写幻灯片内容...")
    try:
        deck = await run_writer(storyline, search_findings, doc_analysis, author)
        deck.meta.title = title
        if author:
            deck.meta.author = author
        _safe_emit("step", f"✍️ 撰写完成: {len(deck.slides)} 页")
    except Exception as e:
        _safe_emit("step", f"⚠️ Writer failed: {e}")
        return PptDomainResult(
            status="error",
            result=f"PPT 内容生成失败: {e}",
            artifact_refs=[],
            review={"passed": False, "reason": str(e)},
            budget={"action": "allow", "reason": "failed before render"},
        )

    # Render
    _safe_emit("step", "📊 正在渲染 PPT...")
    file_id = uuid.uuid4().hex[:8]
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip()[:30] or "report"
    filename = f"{safe_title}_{file_id}.pptx"
    output_path = f"outputs/{filename}"

    try:
        render_pptx(deck, output_path)
        _safe_emit("step", f"✅ PPT 已生成: {filename}")
        _safe_emit("file", json.dumps({"type": "file", "url": f"/download/{filename}", "name": filename}))
    except Exception as e:
        _safe_emit("step", f"⚠️ Render failed: {e}")
        return PptDomainResult(
            status="error",
            result=f"PPT 渲染失败: {e}",
            artifact_refs=[],
            review={"passed": False, "reason": str(e)},
            budget={"action": "allow", "reason": "failed at render"},
        )

    result_text = f"PPT 已生成：《{title}》，共 {len(deck.slides)} 页。\n下载: /download/{filename}"

    # Memory save
    try:
        user_id = input_data.get("user_id", "anonymous")
        memory_store = MemoryStoreV2()
        await memory_store.remember(user_id, query, result_text, intent="ppt_report")
    except Exception as exc:
        _safe_emit("step", f"⚠️ Memory save failed: {exc}")

    return PptDomainResult(
        status="ok",
        result=result_text,
        artifact_refs=[
            {"name": filename, "type": "pptx", "url": f"/download/{filename}"},
        ],
        review={"passed": True, "reason": "PPT rendered successfully"},
        budget={"action": "allow", "reason": "ppt generation complete"},
    )
