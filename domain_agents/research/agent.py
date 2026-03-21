from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import Send
from pydantic import BaseModel
from typing_extensions import TypedDict

import logging

from capabilities.citation_manager import CitationMap
from capabilities.context_manager import ResearchContext
from capabilities.evidence_store import EvidenceCollection, EvidenceItem
from capabilities.memory import ResearchMemory
from capabilities.planner import ResearchPlan, ResearchSubtask, generate_research_plan
from capabilities.review_gates import ReviewResult
from core.content_utils import extract_result_text
from domain_agents.research.reviewers import ResearchReviewGate
from domain_agents.research.schemas import WorkerResult

_log = logging.getLogger("chatdada.research")


def _safe_emit(event_type: str, content: str) -> None:
    """Emit a progress event via LangGraph stream writer, silently no-op outside a graph."""
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        writer({"event_type": event_type, "content": content})
    except Exception:
        pass


class ResearchDomainResult(BaseModel):
    status: str
    result: str
    artifact_refs: list[dict[str, Any]]
    review: dict[str, Any]
    strategy: str


class ParallelResearchState(TypedDict, total=False):
    query: str
    task_id: str
    report_profile: str
    plan: dict[str, Any]
    worker_results: Annotated[list[dict[str, Any]], operator.add]
    final_text: str


def _fallback_plan(query: str) -> ResearchPlan:
    return ResearchPlan(
        original_query=query,
        clarified_goal=query,
        subtasks=[
            ResearchSubtask(
                id="sub_1",
                topic=query,
                search_angles=[query],
                priority=1,
                max_rounds=3,
                completion_criteria="整理出可直接回答原始问题的研究摘要",
            )
        ],
    )


async def _generate_plan(query: str, report_profile: str) -> ResearchPlan:
    try:
        return await generate_research_plan(query, memory_context="", report_profile=report_profile)
    except Exception:
        return _fallback_plan(query)


async def _run_parallel_worker_node(state: ParallelResearchState) -> dict[str, Any]:
    from domain_agents.research.worker import run_worker
    from domain_agents.research.tools import get_research_tools

    subtask = state["subtask"]
    memory = ResearchMemory(state["task_id"]) if state.get("task_id") else None
    try:
        findings = await run_worker(subtask, get_research_tools(), memory)
        result = WorkerResult(
            subtask_id=str(subtask.get("id", "")),
            topic=str(subtask.get("topic", "")),
            status="ok" if findings else "partial",
            findings=findings,
        )
    except Exception as exc:
        result = WorkerResult(
            subtask_id=str(subtask.get("id", "")),
            topic=str(subtask.get("topic", "")),
            status="error",
            error=str(exc),
        )
    return {"worker_results": [result.model_dump()]}


def _fan_out(state: ParallelResearchState) -> list[Send]:
    plan = ResearchPlan.from_dict(state["plan"])
    sends: list[Send] = []
    for subtask in plan.subtasks:
        sends.append(
            Send(
                "parallel_worker",
                {
                    "query": state["query"],
                    "task_id": state.get("task_id", ""),
                    "report_profile": state.get("report_profile", ""),
                    "subtask": subtask.to_dict(),
                },
            )
        )
    return sends


async def _synthesize_parallel_results(state: ParallelResearchState) -> dict[str, Any]:
    from domain_agents.research.utils import _synthesize_parallel_findings

    results = {item["subtask_id"]: item.get("findings", "") for item in state.get("worker_results", [])}
    try:
        final_text = await _synthesize_parallel_findings(
            state["query"],
            results,
            state.get("report_profile", ""),
        )
    except Exception:
        sections = []
        for item in state.get("worker_results", []):
            sections.append(f"## {item.get('topic', item.get('subtask_id', 'subtask'))}\n\n{item.get('findings', item.get('error', ''))}")
        final_text = "\n\n".join(sections)
    return {"final_text": extract_result_text(final_text)}


def build_parallel_research_domain_graph():
    graph = StateGraph(ParallelResearchState)
    graph.add_node("plan", lambda state: {})
    graph.add_node("parallel_worker", _run_parallel_worker_node)
    graph.add_node("synthesize", _synthesize_parallel_results)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", _fan_out, ["parallel_worker"])
    graph.add_edge("parallel_worker", "synthesize")
    graph.add_edge("synthesize", END)
    return graph.compile()


import re

_URL_RE = re.compile(r"https?://[^\s\)\]\"'>]+")


def _build_evidence_and_citations(
    task_id: str,
    final_text: str,
    worker_results: list[dict[str, Any]] | None = None,
) -> tuple[EvidenceCollection, CitationMap]:
    """Extract evidence items and citations from research output."""
    evidence = EvidenceCollection(task_id=task_id)
    citations = CitationMap()

    # Collect from worker results
    for wr in worker_results or []:
        findings = str(wr.get("findings", ""))
        topic = str(wr.get("topic", wr.get("subtask_id", "")))
        for url in _URL_RE.findall(findings):
            evidence.add(EvidenceItem(
                evidence_id=f"ev_{len(evidence.items) + 1}",
                evidence_type="url",
                source=url,
                summary=f"Found during research on: {topic}",
            ))
            citations.add(url, title=topic)

    # Collect URLs from final text
    for url in _URL_RE.findall(final_text):
        evidence.add(EvidenceItem(
            evidence_id=f"ev_{len(evidence.items) + 1}",
            evidence_type="url",
            source=url,
            summary="Extracted from final report",
        ))
        citations.add(url)

    return evidence, citations


def _persist_evidence_and_citations(
    task_dir: Path,
    evidence: EvidenceCollection,
    citations: CitationMap,
) -> list[dict[str, Any]]:
    """Persist evidence.json and citations.json, return artifact refs."""
    import json as _json

    refs: list[dict[str, Any]] = []
    if evidence.items:
        ev_path = task_dir / "evidence.json"
        ev_data = [
            {
                "evidence_id": e.evidence_id,
                "type": e.evidence_type,
                "source": e.source,
                "summary": e.summary,
                "confidence": e.confidence,
            }
            for e in evidence.items
        ]
        ev_path.write_text(_json.dumps(ev_data, ensure_ascii=False, indent=2), encoding="utf-8")
        refs.append({"type": "file", "name": "evidence.json", "path": str(ev_path)})

    if citations.all():
        cit_path = task_dir / "citations.json"
        cit_path.write_text(_json.dumps(citations.to_dicts(), ensure_ascii=False, indent=2), encoding="utf-8")
        refs.append({"type": "file", "name": "citations.json", "path": str(cit_path)})

    return refs


def _collect_artifact_refs(task_id: str) -> list[dict[str, Any]]:
    if not task_id:
        return []
    memory = ResearchMemory(task_id)
    refs: list[dict[str, Any]] = []
    task_dir = memory.task_dir
    for path in (
        *memory.list_findings(),
        task_dir / "summaries" / "latest.md",
        task_dir / "final_report.md",
    ):
        if not path.exists():
            continue
        refs.append(
            {
                "type": "file",
                "name": path.name,
                "path": str(path),
            }
        )
    return refs


async def _run_legacy_research(input_data: dict[str, Any]) -> str:
    from domain_agents.research.legacy_runner import run as deep_research_run

    result = await deep_research_run(input_data)
    return str(result.get("result", result))


async def build_deepagents_research_agent() -> object:
    from deepagents import create_deep_agent

    from domain_agents.research.tools import get_research_tools

    subagents = [
        {
            "name": "web_researcher",
            "description": "Collect evidence from the web for a single research angle.",
            "system_prompt": "Focus on collecting concise evidence with sources.",
            "tools": get_research_tools(),
        },
        {
            "name": "evidence_synthesizer",
            "description": "Synthesize findings into a concise research summary.",
            "system_prompt": "Synthesize evidence into a structured Chinese summary with citations.",
            "tools": get_research_tools(),
        },
    ]
    from core.models import build_chat_model

    return create_deep_agent(
        model=build_chat_model("research_domain"),
        system_prompt="你是 research domain agent，负责规划研究并组织子代理协作。",
        tools=get_research_tools(),
        subagents=subagents,
        checkpointer=False,
        name="research_domain_agent",
    )


async def run_research_domain(input_data: dict[str, Any]) -> ResearchDomainResult:
    query = str(input_data.get("query", input_data.get("task", "")) or "").strip()
    task_id = str(input_data.get("task_id", "") or "")
    report_profile = str(input_data.get("report_profile", "") or "")
    use_parallel = bool(input_data.get("parallel"))
    use_deepagents = bool(input_data.get("use_deepagents", True))

    if task_id:
        ResearchMemory(task_id).init(query, report_profile)

    _safe_emit("step", f"🔬 Research domain started: strategy selection for '{query[:60]}'")

    strategy = "legacy"
    if use_parallel:
        strategy = "graph_parallel"
        _safe_emit("step", "📋 Generating parallel research plan...")
        plan = await _generate_plan(query, report_profile)
        graph = build_parallel_research_domain_graph()
        result = await graph.ainvoke(
            {
                "query": query,
                "task_id": task_id,
                "report_profile": report_profile,
                "plan": plan.to_dict(),
                "worker_results": [],
            }
        )
        final_text = str(result.get("final_text", "") or "")
    elif use_deepagents:
        strategy = "deepagents_harness"
        _safe_emit("step", "🤖 Building deepagents research agent...")
        agent = await build_deepagents_research_agent()
        _safe_emit("step", "🚀 Executing deepagents research...")
        response = await agent.ainvoke(
            {"messages": [HumanMessage(content=f"请深入研究以下主题，并给出中文研究摘要：{query}")]}
        )
        messages = response.get("messages", []) if isinstance(response, dict) else []
        final_text = ""
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                final_text = extract_result_text(getattr(message, "content", ""))
                if final_text:
                    break
    #         if not final_text:
    #             _safe_emit("step", "⚠️ Deepagents produced no output, falling back to legacy research")
    #             final_text = await _run_legacy_research(input_data)
    #             strategy = "legacy_fallback"
        # except Exception:
            # _safe_emit("step", "⚠️ Deepagents failed, falling back to legacy research")
    #         final_text = await _run_legacy_research(input_data)
    #         strategy = "legacy_fallback"
    # else:
    #     final_text = await _run_legacy_research(input_data)

    if task_id:
        ResearchMemory(task_id).save_final_report(final_text)

    # Build evidence collection and citation map
    evidence, citations = _build_evidence_and_citations(task_id, final_text)
    if citations.all():
        final_text = final_text.rstrip() + "\n\n" + citations.render_markdown_references()

    _safe_emit("step", f"✅ Research complete (strategy={strategy}), running review gate...")
    artifact_refs = _collect_artifact_refs(task_id)

    # Persist evidence and citations
    if task_id:
        task_dir = ResearchMemory(task_id).task_dir
        artifact_refs.extend(_persist_evidence_and_citations(task_dir, evidence, citations))

    review: ReviewResult = await ResearchReviewGate().evaluate(
        {"report": final_text, "artifact_refs": artifact_refs}
    )
    return ResearchDomainResult(
        status="ok",
        result=final_text,
        artifact_refs=artifact_refs,
        review={
            "passed": review.passed,
            "issues": [
                {"severity": issue.severity, "message": issue.message, "metadata": issue.metadata}
                for issue in review.issues
            ],
        },
        strategy=strategy,
    )
