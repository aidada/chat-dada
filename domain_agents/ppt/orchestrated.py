"""
PPT domain agent — orchestrated version.

PPT is fundamentally a sequential pipeline (outline → search → write → render).
The orchestrator handles outline+search+write via deepagents; rendering is done
in the wrapper since it's a deterministic non-LLM step.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from capabilities.review_gates import ReviewGate
from domain_agents.ppt.agent import PptDomainResult, STORYLINE_SYSTEM
from domain_agents.research.tools import get_research_tools

from workflows.orchestrator import build_orchestrated_graph
from workflows.spec import DomainSpec, SubagentConfig

_log = logging.getLogger("chatdada.ppt.orchestrated")


# ── PPT-specific system prompt (combines planning + writing) ─────────────────

_PPT_ORCHESTRATED_SYSTEM = """\
你是 PPT 生成专家。你的任务是根据用户需求生成完整的演示文稿内容。

流程：
1. 分析用户需求，规划 PPT 大纲（章节结构）
2. 搜索相关素材和数据
3. 为每一页撰写内容

最终输出一份完整的 PPT 内容（Markdown 格式），包括：
- 封面（标题、副标题）
- 各章节内容页（每页 50-80 字要点 + 讲者备注）
- 总结页

写作原则：
- 每页正文控制在 50-80 字，要点化表达
- 图表用真实数据（从搜索结果中提取）
- 学术报告风格：严谨、有数据支撑
- 使用中文
"""


# ── DomainSpec declaration ───────────────────────────────────────────────────

PPT_SPEC = DomainSpec(
    name="ppt",
    model_role="orchestrator",  # PPT uses orchestrator model role
    system_prompt=_PPT_ORCHESTRATED_SYSTEM,
    tools=get_research_tools(),  # PPT needs search tools for content gathering
    subagents=[
        SubagentConfig(
            name="outline_planner",
            description="Plan the PPT storyline and section structure.",
            system_prompt=STORYLINE_SYSTEM,
            tools=[],
        ),
        SubagentConfig(
            name="content_researcher",
            description="Search for relevant data and materials for slides.",
            system_prompt="搜索与 PPT 主题相关的数据、案例和素材。输出结构化的要点和来源。",
            tools=get_research_tools(),
        ),
    ],
    evaluator=ReviewGate(),  # base ReviewGate — PPT rendering is the real validation
    strategy_hints=["sequential"],  # PPT is a linear pipeline
    max_steps=5,
    max_cost=2.0,
)


# ── Compiled graph ───────────────────────────────────────────────────────────

_graph = build_orchestrated_graph(PPT_SPEC)


# ── Entry point ──────────────────────────────────────────────────────────────

def _safe_emit(event_type: str, content: str) -> None:
    try:
        from langgraph.config import get_stream_writer
        get_stream_writer()({"event_type": event_type, "content": content})
    except Exception:
        pass


async def run_ppt_domain_orchestrated(
    input_data: dict[str, Any],
) -> PptDomainResult:
    """Run PPT domain using the dynamic workflow orchestrator.

    The orchestrator handles content generation (outline + research + writing).
    Rendering to .pptx is done here as a post-processing step.
    """
    query = input_data.get("query") or input_data.get("task", "")
    task_id = input_data.get("task_id", "ppt_unknown")

    _log.info("Starting orchestrated PPT: query=%s task_id=%s", str(query)[:60], task_id)

    result = await _graph.ainvoke({
        "goal": str(query),
        "task_id": str(task_id),
        "report_profile": "",
        "cost": 0.0,
        "progress": 0.0,
        "confidence": 0.0,
        "max_cost": PPT_SPEC.max_cost,
        "max_steps": PPT_SPEC.max_steps,
        "intermediate_results": [],
        "evaluations": [],
        "step_history": [],
        "coverage": {},
    })

    content_text = result.get("final_result", "")
    strategy_trace = result.get("step_history", [])
    strategies_used = [s.get("strategy", "") for s in strategy_trace]

    if not content_text:
        return PptDomainResult(
            status="error",
            result="PPT 内容生成失败。",
            artifact_refs=[],
            review={"passed": False, "reason": "No content generated"},
            budget={"action": "allow", "reason": f"orchestrated({' → '.join(strategies_used)})"},
        )

    # Attempt to render content to .pptx
    try:
        from domain_agents.ppt.writer_agent import run_writer
        from ppt_engine.renderer import render_pptx

        _safe_emit("step", "✍️ 正在生成幻灯片 DSL...")
        deck = await run_writer(
            storyline=content_text,
            search_findings="",
            doc_analysis="",
            author="",
        )

        title = deck.meta.title or str(query)[:30]
        file_id = uuid.uuid4().hex[:8]
        safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip()[:30] or "report"
        filename = f"{safe_title}_{file_id}.pptx"
        output_path = f"outputs/{filename}"

        _safe_emit("step", "📊 正在渲染 PPT...")
        render_pptx(deck, output_path)

        _safe_emit("step", f"✅ PPT 已生成: {filename}")
        _safe_emit("file", json.dumps({"type": "file", "url": f"/download/{filename}", "name": filename}))

        result_text = f"PPT 已生成：《{title}》，共 {len(deck.slides)} 页。\n下载: /download/{filename}"
        return PptDomainResult(
            status="ok",
            result=result_text,
            artifact_refs=[{"name": filename, "type": "pptx", "url": f"/download/{filename}"}],
            review={"passed": True, "reason": "PPT rendered successfully"},
            budget={"action": "allow", "reason": f"orchestrated({' → '.join(strategies_used)})"},
        )
    except Exception as exc:
        _log.warning("PPT rendering failed, returning raw content: %s", exc)
        return PptDomainResult(
            status="ok",
            result=content_text,
            artifact_refs=[],
            review={"passed": True, "reason": f"Content generated, render failed: {exc}"},
            budget={"action": "allow", "reason": f"orchestrated({' → '.join(strategies_used)})"},
        )
