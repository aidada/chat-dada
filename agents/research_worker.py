"""
Research worker — lightweight agent for executing a single research subtask.

Designed to be run in parallel via coordinate_research().
"""
from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Literal

from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from core.models import get_llm

log = logging.getLogger("chatdada.research_worker")

MAX_PARALLEL_WORKERS = 3

# ---------------------------------------------------------------------------
# Worker state
# ---------------------------------------------------------------------------


class WorkerState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    subtask_topic: str
    search_angles: list[str]
    step_count: int
    max_steps: int
    findings: str
    completion_criteria: str


# ---------------------------------------------------------------------------
# Worker nodes
# ---------------------------------------------------------------------------

WORKER_SYSTEM_PROMPT = """你是一个专注的研究助手。你只负责完成一个具体的研究子任务。

策略：
1. 围绕给定的主题和搜索角度进行检索
2. 每轮最多调用 1-2 个工具
3. 不要偏离子任务主题
4. 达到完成标准后，输出研究发现摘要（不调用任何工具）"""


def _build_worker_messages(state: WorkerState) -> list[BaseMessage]:
    """Build messages for the worker LLM."""
    prompt = (
        f"子任务主题：{state['subtask_topic']}\n"
        f"搜索角度：{', '.join(state['search_angles'])}\n"
        f"完成标准：{state['completion_criteria']}\n\n"
    )
    if state.get("findings"):
        prompt += f"当前发现：\n{state['findings']}\n\n"
    prompt += "请继续研究，或输出最终发现摘要。"
    return [
        SystemMessage(content=WORKER_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]


async def worker_planner(state: WorkerState, tools: list) -> dict:
    """Worker planner node — calls LLM with subtask context."""
    llm = get_llm("deep_research").bind_tools(tools)
    messages = _build_worker_messages(state)
    response = await llm.ainvoke(messages)

    # Extract text findings from response
    findings = state.get("findings", "")
    text = _extract_text(response)
    if text:
        findings = _merge_worker_findings(findings, text)

    return {
        "messages": [response],
        "step_count": state["step_count"] + 1,
        "findings": findings,
    }


def worker_should_continue(state: WorkerState) -> Literal["tools", "finish"]:
    """Check if worker should continue or finish."""
    if state["step_count"] >= state["max_steps"]:
        return "finish"
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "finish"


def worker_finish(state: WorkerState) -> dict:
    """Extract final findings from worker."""
    findings = state.get("findings", "")
    # Also try to extract from last AI message
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage):
            text = _extract_text(msg)
            if text and len(text) > len(findings):
                findings = text
            break
    return {"findings": findings}


# ---------------------------------------------------------------------------
# Worker graph
# ---------------------------------------------------------------------------


def build_worker_graph(tools: list):
    """Build a worker graph for a single subtask."""

    async def planner_node(state: WorkerState) -> dict:
        return await worker_planner(state, tools)

    async def tools_node(state: WorkerState) -> dict:
        return await ToolNode(tools).ainvoke(state)

    g = StateGraph(WorkerState)
    g.add_node("planner", planner_node)
    g.add_node("tools", tools_node)
    g.add_node("finish", worker_finish)
    g.set_entry_point("planner")
    g.add_conditional_edges("planner", worker_should_continue, {"tools": "tools", "finish": "finish"})
    g.add_edge("tools", "planner")
    g.add_edge("finish", END)
    return g.compile()


# ---------------------------------------------------------------------------
# Single worker execution
# ---------------------------------------------------------------------------


async def run_worker(subtask_dict: dict, tools: list, memory=None) -> str:
    """Execute a single worker for one subtask, return findings text."""
    graph = build_worker_graph(tools)

    state: WorkerState = {
        "messages": [HumanMessage(content=f"研究子任务：{subtask_dict.get('topic', '')}")],
        "subtask_topic": subtask_dict.get("topic", ""),
        "search_angles": subtask_dict.get("search_angles", []),
        "step_count": 0,
        "max_steps": subtask_dict.get("max_rounds", 3),
        "findings": "",
        "completion_criteria": subtask_dict.get("completion_criteria", ""),
    }

    try:
        result = await graph.ainvoke(state)
        findings = result.get("findings", "")
        if memory and findings:
            try:
                memory.save_finding(
                    0, "worker",
                    subtask_dict.get("topic", ""),
                    findings,
                    [],
                )
            except Exception:
                log.warning("worker memory save failed for %s", subtask_dict.get("id", ""))
        return findings
    except Exception as e:
        log.warning("worker failed for subtask %s: %s", subtask_dict.get("id", ""), e, exc_info=True)
        return f"子任务 {subtask_dict.get('topic', '')} 执行失败: {e}"


# ---------------------------------------------------------------------------
# Coordinator — wave-based parallel execution
# ---------------------------------------------------------------------------


async def coordinate_research(plan, tools: list, memory=None) -> dict[str, str]:
    """Wave-based parallel execution of research subtasks.

    Returns {subtask_id: findings_text}.
    """
    from research_planner import get_next_subtask

    results: dict[str, str] = {}
    semaphore = asyncio.Semaphore(MAX_PARALLEL_WORKERS)

    async def _run_with_semaphore(subtask_dict: dict) -> tuple[str, str]:
        async with semaphore:
            findings = await run_worker(subtask_dict, tools, memory)
            return subtask_dict["id"], findings

    # Wave-based execution
    max_waves = 10  # safety limit
    for wave in range(max_waves):
        # Collect all eligible subtasks
        eligible: list[dict] = []
        completed_ids = {st.id for st in plan.subtasks if st.status in ("completed", "skipped")}

        for st in plan.subtasks:
            if st.status != "pending":
                continue
            if all(dep in completed_ids for dep in st.depends_on):
                eligible.append(st.to_dict())
                st.status = "in_progress"

        if not eligible:
            break

        # Run wave
        tasks = [_run_with_semaphore(sd) for sd in eligible]
        wave_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        for i, result in enumerate(wave_results):
            subtask_id = eligible[i]["id"]
            if isinstance(result, Exception):
                log.warning("Worker %s failed: %s", subtask_id, result)
                findings = f"执行失败: {result}"
            else:
                _, findings = result

            results[subtask_id] = findings

            # Mark subtask as completed
            for st in plan.subtasks:
                if st.id == subtask_id:
                    st.status = "completed"
                    st.findings_summary = findings[:500] if findings else ""
                    break

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_text(message: AIMessage) -> str:
    """Extract text content from an AIMessage."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content)


def _merge_worker_findings(existing: str, incoming: str, max_chars: int = 3000) -> str:
    """Merge findings, capping at max_chars."""
    parts = [p.strip() for p in (existing, incoming) if p and p.strip()]
    if not parts:
        return ""
    merged = "\n\n".join(parts)
    if len(merged) <= max_chars:
        return merged
    return merged[-max_chars:]
