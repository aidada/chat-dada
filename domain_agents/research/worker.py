"""科研工作流中的模块执行器。

每个 worker 只负责一个模块，输入是模块计划和依赖上下文，
输出是可直接进入评估/聚合阶段的模块草案。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Annotated, Literal

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict

from core.content_utils import extract_result_text, normalize_markdown_report
from core.models import get_llm
from domain_agents.research.config import ResearchConfig
from domain_agents.research.prompts import build_module_worker_messages
from domain_agents.research.schemas import ResearchModuleDraft, WorkerResult
from tools.research_notes import set_research_context
from domain_agents.research.utils import (
    build_citation_bank,
    build_evidence_records,
    merge_evidence,
    module_dependency_context,
)

log = logging.getLogger("chatdada.research_worker")


class WorkerState(TypedDict, total=False):
    """单个模块 worker 的最小状态。"""

    messages: Annotated[list[BaseMessage], add_messages]
    module_plan: dict[str, Any]
    brief: dict[str, Any]
    dependency_context: str
    existing_draft: str
    revision_instructions: str
    findings: str
    step_count: int
    max_steps: int


def _build_worker_messages(state: WorkerState) -> list[BaseMessage]:
    """把模块计划和上下文整理成当前 worker 的提示词。"""
    return build_module_worker_messages(
        state.get("module_plan", {}),
        state.get("brief", {}),
        state.get("dependency_context", ""),
        existing_draft=state.get("existing_draft", ""),
        revision_instructions=state.get("revision_instructions", ""),
    )


async def worker_planner(state: WorkerState, tools: list[Any]) -> dict[str, Any]:
    """模块执行主节点：决定继续查证还是直接给出模块草案。"""
    llm = get_llm("research_domain").bind_tools(tools)
    response = await llm.ainvoke(_build_worker_messages(state))

    findings = state.get("findings", "")
    text = extract_result_text(response)
    if text:
        findings = normalize_markdown_report(text)

    return {
        "messages": [response],
        "findings": findings,
        "step_count": int(state.get("step_count", 0)) + 1,
    }


def worker_should_continue(state: WorkerState) -> Literal["tools", "finish"]:
    """如果模型还在请求工具，就继续；否则收敛到 finish。"""
    last = state.get("messages", [])[-1] if state.get("messages") else None
    if isinstance(last, AIMessage) and last.tool_calls:
        # 允许最后一轮 planner 先把工具调用发出去，再给模型一次收束成正文的机会，
        # 否则会出现“最后一轮只拿到 tool_calls 就被直接 finish”的空草案。
        if int(state.get("step_count", 0)) <= int(state.get("max_steps", 1)):
            return "tools"
        return "finish"
    return "finish"


def worker_finish(state: WorkerState) -> dict[str, Any]:
    """统一抽取 worker 最终文本，保证返回值稳定。"""
    findings = normalize_markdown_report(str(state.get("findings", "") or ""))
    if findings:
        return {"findings": findings}

    for message in reversed(state.get("messages", [])):
        if isinstance(message, AIMessage):
            text = extract_result_text(message)
            if text:
                return {"findings": normalize_markdown_report(text)}
    return {"findings": ""}


def build_worker_graph(tools: list[Any]) -> Any:
    """构建单模块 worker 图。

    图结构很简单：planner -> tools -> planner ... -> finish
    """

    async def planner_node(state: WorkerState) -> dict[str, Any]:
        return await worker_planner(state, tools)

    async def tools_node(state: WorkerState) -> dict[str, Any]:
        return await ToolNode(tools).ainvoke(state)

    graph = StateGraph(WorkerState)
    graph.add_node("planner", planner_node)
    graph.add_node("tools", tools_node)
    graph.add_node("finish", worker_finish)
    graph.set_entry_point("planner")
    graph.add_conditional_edges("planner", worker_should_continue, {"tools": "tools", "finish": "finish"})
    graph.add_edge("tools", "planner")
    graph.add_edge("finish", END)
    return graph.compile()


async def run_worker(
    module_dict: dict[str, Any],
    brief: dict[str, Any] | None = None,
    tools: list[Any] | None = None,
    dependency_context: str = "",
    existing_draft: str = "",
    revision_instructions: str = "",
    memory: Any = None,
    step_index: int = 0,
    max_rounds: int | None = None,
) -> dict[str, Any]:
    """运行单个模块 worker，并返回结构化结果。"""

    graph = build_worker_graph(tools or [])
    module_id = str(module_dict.get("module_id") or module_dict.get("id") or "module")
    title = str(module_dict.get("title") or module_dict.get("topic") or module_id)
    owner_role = str(module_dict.get("owner_role") or "argument_worker")
    objective = str(module_dict.get("objective") or module_dict.get("completion_criteria") or title)

    state: WorkerState = {
        "messages": [],
        "module_plan": {
            "module_id": module_id,
            "title": title,
            "owner_role": owner_role,
            "objective": objective,
            "depends_on": list(module_dict.get("depends_on", [])),
            "required_evidence": list(module_dict.get("required_evidence", [])),
            "required_output_fields": list(module_dict.get("required_output_fields", [])),
        },
        "brief": brief or {},
        "dependency_context": dependency_context,
        "existing_draft": existing_draft,
        "revision_instructions": revision_instructions,
        "findings": "",
        "step_count": 0,
        "max_steps": max_rounds or int(module_dict.get("max_rounds", 0) or ResearchConfig().max_worker_rounds),
    }

    try:
        set_research_context(memory, step_index)
        result = await graph.ainvoke(state)
        findings = normalize_markdown_report(str(result.get("findings", "") or ""))
        evidence = build_evidence_records(module_id, title, findings)
        urls = [item.get("url", "") for item in evidence if item.get("url")]
        # 这里仍然把每个模块的草案写入持久化 memory，
        # 便于后续回看每一轮模块执行产物。
        if memory is not None and findings:
            try:
                memory.save_finding(step_index, module_id, objective, findings, urls)
            except Exception:
                log.warning("Failed to persist worker finding for %s", module_id, exc_info=True)
        return WorkerResult(
            module_id=module_id,
            topic=title,
            status="ok" if findings else "partial",
            findings=findings,
            evidence=evidence,
        ).model_dump()
    except Exception as exc:
        log.warning("Worker failed for module %s", module_id, exc_info=True)
        return WorkerResult(
            module_id=module_id,
            topic=title,
            status="error",
            findings="",
            evidence=[],
            error=str(exc),
        ).model_dump()


async def coordinate_modules(
    plan: dict[str, Any],
    brief: dict[str, Any],
    module_outputs: dict[str, dict[str, Any]],
    module_status: dict[str, str],
    revision_targets: list[dict[str, Any]],
    tools: list[Any],
    memory: Any = None,
    config: ResearchConfig | None = None,
    optimizer_context: str = "",
) -> dict[str, Any]:
    """按依赖关系调度多个模块。

    这里不是简单并发全部模块，而是按 wave 执行：
    只有依赖已经完成/锁定的模块才会进入当前轮。
    """

    cfg = config or ResearchConfig()
    semaphore = asyncio.Semaphore(cfg.max_parallel_workers)
    revision_map = {target["module_id"]: target for target in revision_targets if target.get("module_id")}

    outputs = dict(module_outputs)
    status = dict(module_status)
    evidence_bank: list[dict[str, Any]] = []
    worker_results: list[dict[str, Any]] = []
    modules = [dict(item) for item in plan.get("modules", [])]

    async def _run_one(module: dict[str, Any], step_index: int) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            module_id = str(module["module_id"])
            dependency_text = module_dependency_context(module, outputs)
            target = revision_map.get(module_id, {})
            instructions = optimizer_context.strip()
            # 如果当前模块来自 revision_targets，就把评估原因和保留约束灌进去，
            # 这样模块执行器会更像“定向修订”而不是“重写全文”。
            if target:
                action_text = "; ".join(target.get("actions", []))
                instructions = "\n".join(
                    part for part in (
                        instructions,
                        f"低分原因：{target.get('reason', '')}",
                        f"修订动作：{action_text}",
                        f"必须保留：{'; '.join(target.get('preserve_constraints', []))}",
                    ) if part
                )
            result = await run_worker(
                module,
                brief=brief,
                tools=tools,
                dependency_context=dependency_text,
                existing_draft=str((outputs.get(module_id) or {}).get("content", "") or ""),
                revision_instructions=instructions,
                memory=memory,
                step_index=step_index,
                max_rounds=cfg.max_worker_rounds,
            )
            return module_id, result

    max_waves = 12
    step_index = 1
    for _ in range(max_waves):
        eligible: list[dict[str, Any]] = []
        # 当前轮只挑出依赖已经满足的模块，避免方法/实验模块抢跑。
        for module in modules:
            module_id = str(module["module_id"])
            current_status = status.get(module_id, "pending")
            if current_status not in {"pending", "needs_revision"}:
                continue
            deps = module.get("depends_on", [])
            if all(status.get(dep) in {"completed", "locked"} for dep in deps):
                eligible.append(module)

        if not eligible:
            break

        # 先把当前轮模块标记为运行中，再并发执行。
        for module in eligible:
            status[module["module_id"]] = "running"

        results = await asyncio.gather(*[_run_one(module, step_index + idx) for idx, module in enumerate(eligible)])
        step_index += len(eligible)

        for module_id, result in results:
            worker_results.append(result)
            evidence_bank = merge_evidence(evidence_bank, list(result.get("evidence", [])))
            result_status = str(result.get("status", "") or "")
            findings = normalize_markdown_report(str(result.get("findings", "") or ""))
            if result_status == "error":
                status[module_id] = "needs_revision"
                continue
            if not findings:
                # 空草案不能算完成；保留已有版本，等待后续重试或人工纠偏。
                status[module_id] = "needs_revision"
                continue

            previous = outputs.get(module_id) or {}
            version = int(previous.get("version", 0) or 0) + 1
            # 每次成功执行都写回最新模块草案，并递增版本号，
            # 这样后续评估和定向修订都有明确的模块快照可用。
            draft = ResearchModuleDraft(
                module_id=module_id,
                version=version,
                status="completed",
                content=findings,
                evidence_ids=[item.get("evidence_id", "") for item in result.get("evidence", []) if item.get("evidence_id")],
                citation_ids=[str(idx) for idx, _ in enumerate(result.get("evidence", []), start=1)],
                open_gaps=[],
                assumptions=[],
                last_worker_role=str(next((module["owner_role"] for module in modules if module["module_id"] == module_id), "argument_worker")),
                last_review_score=float(previous.get("last_review_score", 0.0) or 0.0),
                locked=False,
            )
            outputs[module_id] = draft.model_dump()
            status[module_id] = "completed"

    return {
        "module_outputs": outputs,
        "module_status": status,
        "evidence_bank": evidence_bank,
        "citation_bank": build_citation_bank(evidence_bank),
        "worker_results": worker_results,
    }
