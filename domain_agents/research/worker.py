"""科研工作流中的模块执行器。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Annotated, Literal

import httpx
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import RetryPolicy
from typing_extensions import TypedDict

from capabilities.retrieval_cache import (
    RetrievalCache,
    RetrievalCacheEntry,
    build_query_fingerprint,
)
from core.content_utils import extract_result_text, normalize_markdown_report
from core.logger import record_monitor_event
from core.models import get_llm
from domain_agents.research.config import ResearchConfig
from domain_agents.research.prompts import (
    build_draft_worker_messages,
    build_search_worker_messages,
    build_validate_worker_messages,
)
from domain_agents.research.schemas import ResearchModuleDraft, WorkerResult
from domain_agents.research.utils import (
    build_citation_bank,
    build_evidence_records,
    collect_urls,
    extract_json_payload,
    merge_evidence,
    module_dependency_context,
)
from tools.research_notes import set_research_context

log = logging.getLogger("chatdada.research_worker")

DRAFT_MODULE_MAX_ATTEMPTS = 3


class WorkerState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    module_plan: dict[str, Any]
    brief: dict[str, Any]
    dependency_context: str
    existing_draft: str
    revision_instructions: str
    findings: str
    evidence_pack: list[dict[str, Any]]
    search_history: list[dict[str, Any]]
    query_fingerprints: dict[str, dict[str, Any]]
    draft_status: str
    blocker_reason: str
    validation_notes: list[str]
    last_tool_results: list[dict[str, Any]]
    last_search_metrics: dict[str, Any]
    search_round: int
    max_search_rounds: int


def _tool_map(tools: list[Any]) -> dict[str, Any]:
    return {str(getattr(tool, "name", "")): tool for tool in tools if getattr(tool, "name", None)}


def _remaining_search_rounds(state: WorkerState) -> int:
    return max(int(state.get("max_search_rounds", 0) or 0) - int(state.get("search_round", 0) or 0), 0)


def _extract_query(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name == "browser_navigate":
        return str(args.get("task_description", "") or "")
    return str(args.get("query", "") or args.get("task_description", "") or "")


def _build_search_messages(state: WorkerState) -> list[BaseMessage]:
    return build_search_worker_messages(
        state.get("module_plan", {}),
        state.get("brief", {}),
        state.get("dependency_context", ""),
        evidence_pack=list(state.get("evidence_pack", []) or []),
        search_history=list(state.get("search_history", []) or []),
        existing_draft=state.get("existing_draft", ""),
        revision_instructions=state.get("revision_instructions", ""),
        remaining_search_rounds=_remaining_search_rounds(state),
    )


def _build_draft_messages(state: WorkerState) -> list[BaseMessage]:
    return build_draft_worker_messages(
        state.get("module_plan", {}),
        state.get("brief", {}),
        state.get("dependency_context", ""),
        evidence_pack=list(state.get("evidence_pack", []) or []),
        existing_draft=state.get("existing_draft", ""),
        revision_instructions=state.get("revision_instructions", ""),
    )


def _make_tool_evidence(
    module_id: str,
    title: str,
    tool_name: str,
    query: str,
    result_text: str,
) -> list[dict[str, Any]]:
    evidence = build_evidence_records(module_id, title, result_text)
    if evidence:
        for item in evidence:
            item.setdefault("tool_name", tool_name)
            item.setdefault("query", query)
        return evidence
    if not str(result_text or "").strip():
        return []
    fingerprint = build_query_fingerprint(tool_name=tool_name, query=query)
    return [
        {
            "evidence_id": f"{module_id}_{tool_name}_{fingerprint[:8]}",
            "title": f"{title} / {tool_name}",
            "url": "",
            "source_type": tool_name,
            "snippet": str(result_text or "")[:400],
            "claim_supported": query or title,
            "relevance_score": 0.5,
            "recency_score": 0.0,
            "traceable": False,
            "tool_name": tool_name,
            "query": query,
        }
    ]


async def _invoke_named_tool(tool: Any, payload: dict[str, Any]) -> str:
    result = await tool.ainvoke(payload)
    if isinstance(result, str):
        return result
    return str(result or "")


async def _execute_tool_call(tool_name: str, args: dict[str, Any], tools: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    query = _extract_query(tool_name, args)
    metadata: dict[str, Any] = {}

    if tool_name == "academic_search":
        from tools.academic_search import run as search_academic

        base_result = await search_academic({"query": query})
        text = str(base_result.get("result", "") or "")
        metadata = {key: value for key, value in base_result.items() if key != "result"}
        fallback_tools: list[str] = []
        fallback_triggered = (
            str(metadata.get("status", "") or "") != "ok"
            or int(metadata.get("total_results", 0) or 0) <= 0
            or "http 429" in text.lower()
            or "no results" in text.lower()
        )
        if fallback_triggered and tools.get("exa_deep_search") is not None:
            exa_text = await _invoke_named_tool(
                tools["exa_deep_search"],
                {
                    "query": query,
                    "mode": "summary",
                    "category": "research paper",
                    "summary_query": query,
                },
            )
            if exa_text.strip():
                fallback_tools.append("exa_deep_search")
                text = "\n\n".join(part for part in (text.strip(), f"## Exa Fallback\n{exa_text.strip()}") if part)
        if not collect_urls(text) and tools.get("browser_navigate") is not None:
            browser_text = await _invoke_named_tool(
                tools["browser_navigate"],
                {"task_description": f"Find traceable academic sources about: {query}"},
            )
            if browser_text.strip():
                fallback_tools.append("browser_navigate")
                text = "\n\n".join(part for part in (text.strip(), f"## Browser Fallback\n{browser_text.strip()}") if part)
        metadata["fallback_tools"] = fallback_tools
        metadata["fallback_triggered"] = bool(fallback_tools)
        return text, metadata

    tool = tools.get(tool_name)
    if tool is None:
        raise RuntimeError(f"Unknown worker tool: {tool_name}")
    text = await _invoke_named_tool(tool, args)
    return text, metadata


async def plan_search(state: WorkerState, tools: list[Any]) -> dict[str, Any]:
    llm = get_llm("research_domain").bind_tools(tools)
    response = await llm.ainvoke(_build_search_messages(state))
    return {"messages": [response]}


def _route_after_plan(state: WorkerState) -> Literal["run_tools", "draft_module"]:
    last = state.get("messages", [])[-1] if state.get("messages") else None
    if (
        isinstance(last, AIMessage)
        and last.tool_calls
        and int(state.get("search_round", 0) or 0) < int(state.get("max_search_rounds", 0) or 0)
    ):
        return "run_tools"
    return "draft_module"


async def run_tools(state: WorkerState, tools: list[Any]) -> dict[str, Any]:
    last = state.get("messages", [])[-1] if state.get("messages") else None
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {"last_tool_results": []}

    cache = RetrievalCache(state.get("query_fingerprints"))
    tool_lookup = _tool_map(tools)
    module_plan = dict(state.get("module_plan", {}) or {})
    module_id = str(module_plan.get("module_id", "") or "module")
    title = str(module_plan.get("title", "") or module_id)

    tool_messages: list[ToolMessage] = []
    tool_results: list[dict[str, Any]] = []

    for call in last.tool_calls:
        tool_name = str(call.get("name", "") or "")
        args = call.get("args", {}) if isinstance(call.get("args"), dict) else {}
        query = _extract_query(tool_name, args)
        fingerprint = build_query_fingerprint(
            tool_name=tool_name,
            query=query,
            mode=str(args.get("mode", "") or ""),
            category=str(args.get("category", "") or ""),
            summary_query=str(args.get("summary_query", "") or query),
        )

        cached = cache.get(fingerprint)
        cache_hit = cached is not None
        if cached is not None:
            result_text = str(cached.get("result", "") or "")
            evidence = list(cached.get("evidence", []) or [])
            metadata = dict(cached.get("metadata", {}) or {})
        else:
            result_text, metadata = await _execute_tool_call(tool_name, args, tool_lookup)
            evidence = _make_tool_evidence(module_id, title, tool_name, query, result_text)
            cache.put(
                RetrievalCacheEntry(
                    fingerprint=fingerprint,
                    tool_name=tool_name,
                    query=query,
                    mode=str(args.get("mode", "") or ""),
                    category=str(args.get("category", "") or ""),
                    summary_query=str(args.get("summary_query", "") or query),
                    result=result_text,
                    evidence=evidence,
                    metadata=metadata,
                )
            )

        fallback_tools = list(metadata.get("fallback_tools", []) or [])
        tool_messages.append(
            ToolMessage(
                content=result_text or "(empty tool result)",
                tool_call_id=str(call.get("id", "") or ""),
                name=tool_name,
            )
        )
        tool_results.append(
            {
                "tool_name": tool_name,
                "query": query,
                "fingerprint": fingerprint,
                "cache_hit": cache_hit,
                "result": result_text,
                "evidence": evidence,
                "fallback_tools": fallback_tools,
                "new_evidence_count": 0 if cache_hit else len(evidence),
                "duplicate_hit_count": 1 if cache_hit else 0,
                "success_result_count": len(collect_urls(result_text)) or (1 if result_text.strip() else 0),
                "metadata": metadata,
            }
        )

    return {
        "messages": tool_messages,
        "last_tool_results": tool_results,
        "query_fingerprints": cache.export(),
        "search_round": int(state.get("search_round", 0) or 0) + 1,
    }


async def integrate_evidence(state: WorkerState) -> dict[str, Any]:
    existing = list(state.get("evidence_pack", []) or [])
    history = list(state.get("search_history", []) or [])
    last_tool_results = list(state.get("last_tool_results", []) or [])

    evidence_pack = list(existing)
    new_evidence_total = 0
    duplicate_hit_total = 0
    fallback_total = 0
    success_result_total = 0

    for item in last_tool_results:
        before = len(evidence_pack)
        evidence_pack = merge_evidence(evidence_pack, list(item.get("evidence", []) or []))
        added = max(len(evidence_pack) - before, 0)
        record = {
            "tool_name": item.get("tool_name", ""),
            "query": item.get("query", ""),
            "fingerprint": item.get("fingerprint", ""),
            "cache_hit": bool(item.get("cache_hit")),
            "new_evidence_count": added,
            "duplicate_hit_count": int(item.get("duplicate_hit_count", 0) or 0),
            "success_result_count": int(item.get("success_result_count", 0) or 0),
            "fallback_tools": list(item.get("fallback_tools", []) or []),
        }
        history.append(record)
        new_evidence_total += added
        duplicate_hit_total += record["duplicate_hit_count"]
        success_result_total += record["success_result_count"]
        fallback_total += len(record["fallback_tools"])

    log.info(
        "Worker evidence integrated: module=%s search_round=%s success_results=%s new_evidence=%s duplicate_hits=%s fallback_count=%s",
        state.get("module_plan", {}).get("module_id", ""),
        state.get("search_round", 0),
        success_result_total,
        new_evidence_total,
        duplicate_hit_total,
        fallback_total,
    )
    record_monitor_event(
        layer="agent",
        name="research_worker_convergence",
        event="end",
        metadata={
            "module_id": state.get("module_plan", {}).get("module_id", ""),
            "search_round": int(state.get("search_round", 0) or 0),
            "success_result_count": success_result_total,
            "new_evidence_count": new_evidence_total,
            "duplicate_hit_count": duplicate_hit_total,
            "fallback_count": fallback_total,
        },
    )

    return {
        "evidence_pack": evidence_pack,
        "search_history": history,
        "last_search_metrics": {
            "success_result_count": success_result_total,
            "new_evidence_count": new_evidence_total,
            "duplicate_hit_count": duplicate_hit_total,
            "fallback_count": fallback_total,
        },
    }


def _route_after_integrate(state: WorkerState) -> Literal["plan_search", "draft_module"]:
    if _remaining_search_rounds(state) <= 0:
        return "draft_module"
    return "plan_search"


async def draft_module(state: WorkerState) -> dict[str, Any]:
    llm = get_llm("research_domain")
    response = await llm.ainvoke(_build_draft_messages(state))
    findings = normalize_markdown_report(extract_result_text(response))
    return {
        "messages": [response],
        "findings": findings,
    }


def _should_retry_draft_module(exc: Exception) -> bool:
    transient_httpx_errors = (
        httpx.TimeoutException,
        httpx.NetworkError,
        httpx.ProtocolError,
    )
    if isinstance(exc, transient_httpx_errors):
        return True

    name = exc.__class__.__name__
    module = exc.__class__.__module__
    if name in {"APIConnectionError", "APITimeoutError", "InternalServerError"}:
        return module.startswith("openai")

    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "remoteprotocolerror",
            "incomplete chunked read",
            "peer closed connection",
            "connection reset by peer",
        )
    )


def _fallback_validation(state: WorkerState, findings: str) -> dict[str, Any]:
    module_plan = dict(state.get("module_plan", {}) or {})
    required_fields = [str(item).strip() for item in module_plan.get("required_output_fields", []) if str(item).strip()]
    evidence_pack = list(state.get("evidence_pack", []) or [])
    search_history = list(state.get("search_history", []) or [])
    remaining = _remaining_search_rounds(state)

    stripped = str(findings or "").strip()
    non_heading = stripped.replace("#", "").replace("*", "").replace("-", "").strip()
    missing_requirements: list[str] = []

    if required_fields and len(non_heading) < 120:
        missing_requirements = required_fields[:]

    if not non_heading and not evidence_pack and not search_history:
        return {
            "status": "blocked",
            "reason": "没有检索到任何证据，且未形成模块正文。",
            "missing_requirements": missing_requirements[:3],
            "blocker_reason": "worker 在无工具输出的情况下未能收束为模块正文。",
        }

    if len(non_heading) >= 80:
        return {
            "status": "completed",
            "reason": "模块已形成非空正文。",
            "missing_requirements": missing_requirements[:2],
            "blocker_reason": "",
        }

    last_metrics = dict(state.get("last_search_metrics", {}) or {})
    no_progress = int(last_metrics.get("new_evidence_count", 0) or 0) <= 0 and bool(search_history)
    if remaining > 0 and (evidence_pack or not no_progress):
        return {
            "status": "needs_more_evidence",
            "reason": "草稿仍偏空，需要再补一轮证据后重写。",
            "missing_requirements": missing_requirements[:3],
            "blocker_reason": "",
        }

    return {
        "status": "blocked",
        "reason": "在现有预算内无法稳定收敛为模块正文。",
        "missing_requirements": missing_requirements[:3],
        "blocker_reason": "达到检索预算后仍未形成可用模块正文。",
    }


async def validate_module(state: WorkerState) -> dict[str, Any]:
    findings = normalize_markdown_report(str(state.get("findings", "") or ""))
    payload = None
    try:
        response = await get_llm("research_domain").ainvoke(
            build_validate_worker_messages(
                state.get("module_plan", {}),
                state.get("brief", {}),
                findings,
                list(state.get("evidence_pack", []) or []),
                list(state.get("search_history", []) or []),
                remaining_search_rounds=_remaining_search_rounds(state),
            )
        )
        payload = extract_json_payload(extract_result_text(response))
    except Exception:
        log.warning("Worker validation LLM failed; falling back to heuristics", exc_info=True)

    if not isinstance(payload, dict) or str(payload.get("status", "") or "") not in {
        "completed",
        "needs_more_evidence",
        "blocked",
    }:
        payload = _fallback_validation(state, findings)

    status = str(payload.get("status", "") or "blocked")
    blocker_reason = str(payload.get("blocker_reason", "") or "")
    if status == "needs_more_evidence" and _remaining_search_rounds(state) <= 0:
        status = "blocked"
        blocker_reason = blocker_reason or "达到检索预算后仍需要更多证据，模块已阻塞。"

    return {
        "findings": findings,
        "draft_status": status,
        "blocker_reason": blocker_reason,
        "validation_notes": [str(item) for item in payload.get("missing_requirements", []) if str(item).strip()],
    }


def _route_after_validate(state: WorkerState) -> Literal["plan_search", "completed", "blocked"]:
    status = str(state.get("draft_status", "") or "blocked")
    if status == "completed":
        return "completed"
    if status == "needs_more_evidence" and _remaining_search_rounds(state) > 0:
        return "plan_search"
    return "blocked"


def build_worker_graph(tools: list[Any]) -> Any:
    async def plan_node(state: WorkerState) -> dict[str, Any]:
        return await plan_search(state, tools)

    async def tools_node(state: WorkerState) -> dict[str, Any]:
        return await run_tools(state, tools)

    graph = StateGraph(WorkerState)
    graph.add_node("plan_search", plan_node)
    graph.add_node("run_tools", tools_node)
    graph.add_node("integrate_evidence", integrate_evidence)
    graph.add_node(
        "draft_module",
        draft_module,
        retry_policy=RetryPolicy(
            max_attempts=DRAFT_MODULE_MAX_ATTEMPTS,
            retry_on=_should_retry_draft_module,
        ),
    )
    graph.add_node("validate_module", validate_module)
    graph.set_entry_point("plan_search")
    graph.add_conditional_edges(
        "plan_search",
        _route_after_plan,
        {
            "run_tools": "run_tools",
            "draft_module": "draft_module",
        },
    )
    graph.add_edge("run_tools", "integrate_evidence")
    graph.add_conditional_edges(
        "integrate_evidence",
        _route_after_integrate,
        {
            "plan_search": "plan_search",
            "draft_module": "draft_module",
        },
    )
    graph.add_edge("draft_module", "validate_module")
    graph.add_conditional_edges(
        "validate_module",
        _route_after_validate,
        {
            "plan_search": "plan_search",
            "completed": END,
            "blocked": END,
        },
    )
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
    graph = build_worker_graph(tools or [])
    module_id = str(module_dict.get("module_id") or module_dict.get("id") or "module")
    title = str(module_dict.get("title") or module_dict.get("topic") or module_id)
    owner_role = str(module_dict.get("owner_role") or "argument_worker")
    objective = str(module_dict.get("objective") or module_dict.get("completion_criteria") or title)
    cfg = ResearchConfig()
    max_search_rounds = (
        int(max_rounds)
        if max_rounds is not None
        else cfg.search_budget_for(module_id, owner_role)
    )

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
        "evidence_pack": [],
        "search_history": [],
        "query_fingerprints": {},
        "draft_status": "pending",
        "blocker_reason": "",
        "validation_notes": [],
        "last_tool_results": [],
        "last_search_metrics": {},
        "search_round": 0,
        "max_search_rounds": max_search_rounds,
    }

    try:
        set_research_context(memory, step_index)
        result = await graph.ainvoke(state)
        findings = normalize_markdown_report(str(result.get("findings", "") or ""))
        evidence = merge_evidence(
            list(result.get("evidence_pack", []) or []),
            build_evidence_records(module_id, title, findings),
        )
        urls = [item.get("url", "") for item in evidence if item.get("url")]
        blocker_reason = str(result.get("blocker_reason", "") or "")
        draft_status = str(result.get("draft_status", "") or "blocked")
        search_history = list(result.get("search_history", []) or [])
        search_stats = {
            "search_rounds": int(result.get("search_round", 0) or 0),
            "new_evidence_total": sum(int(item.get("new_evidence_count", 0) or 0) for item in search_history),
            "duplicate_hit_total": sum(int(item.get("duplicate_hit_count", 0) or 0) for item in search_history),
            "fallback_total": sum(len(item.get("fallback_tools", []) or []) for item in search_history),
        }
        if memory is not None and findings:
            try:
                memory.save_finding(step_index, module_id, objective, findings, urls)
            except Exception:
                log.warning("Failed to persist worker finding for %s", module_id, exc_info=True)
        return WorkerResult(
            module_id=module_id,
            topic=title,
            status=draft_status,
            findings=findings,
            evidence=evidence,
            blocker_reason=blocker_reason,
            search_stats=search_stats,
        ).model_dump()
    except Exception as exc:
        log.warning("Worker failed for module %s", module_id, exc_info=True)
        return WorkerResult(
            module_id=module_id,
            topic=title,
            status="error",
            findings="",
            evidence=[],
            blocker_reason="worker_exception",
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
                max_rounds=cfg.search_budget_for(module_id, str(module.get("owner_role", "") or "")),
            )
            return module_id, result

    max_waves = 12
    step_index = 1
    for _ in range(max_waves):
        eligible: list[dict[str, Any]] = []
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

        for module in eligible:
            status[module["module_id"]] = "running"

        results = await asyncio.gather(*[_run_one(module, step_index + idx) for idx, module in enumerate(eligible)])
        step_index += len(eligible)

        for module_id, result in results:
            worker_results.append(result)
            evidence_bank = merge_evidence(evidence_bank, list(result.get("evidence", []) or []))
            result_status = str(result.get("status", "") or "")
            findings = normalize_markdown_report(str(result.get("findings", "") or ""))
            blocker_reason = str(result.get("blocker_reason", "") or "")
            previous = outputs.get(module_id) or {}
            version = int(previous.get("version", 0) or 0) + 1
            worker_role = str(
                next((module["owner_role"] for module in modules if module["module_id"] == module_id), "argument_worker")
            )
            evidence_ids = [item.get("evidence_id", "") for item in result.get("evidence", []) if item.get("evidence_id")]
            citation_ids = [str(idx) for idx, _ in enumerate(result.get("evidence", []), start=1)]

            if result_status == "error":
                status[module_id] = "needs_revision"
                continue

            if result_status == "blocked":
                outputs[module_id] = ResearchModuleDraft(
                    module_id=module_id,
                    version=version,
                    status="blocked",
                    content=findings,
                    evidence_ids=evidence_ids,
                    citation_ids=citation_ids,
                    open_gaps=[blocker_reason] if blocker_reason else [],
                    assumptions=[],
                    last_worker_role=worker_role,
                    last_review_score=float(previous.get("last_review_score", 0.0) or 0.0),
                    locked=False,
                ).model_dump()
                status[module_id] = "blocked"
                continue

            if not findings:
                status[module_id] = "needs_revision"
                continue

            outputs[module_id] = ResearchModuleDraft(
                module_id=module_id,
                version=version,
                status="completed",
                content=findings,
                evidence_ids=evidence_ids,
                citation_ids=citation_ids,
                open_gaps=[],
                assumptions=[],
                last_worker_role=worker_role,
                last_review_score=float(previous.get("last_review_score", 0.0) or 0.0),
                locked=False,
            ).model_dump()
            status[module_id] = "completed"

    changed = True
    while changed:
        changed = False
        for module in modules:
            module_id = str(module["module_id"])
            if status.get(module_id) not in {"pending", "needs_revision"}:
                continue
            blocked_deps = [
                dep
                for dep in module.get("depends_on", [])
                if status.get(dep) in {"blocked", "skipped"}
            ]
            if not blocked_deps:
                continue
            previous = outputs.get(module_id) or {}
            outputs[module_id] = ResearchModuleDraft(
                module_id=module_id,
                version=int(previous.get("version", 0) or 0),
                status="blocked",
                content=str(previous.get("content", "") or ""),
                evidence_ids=list(previous.get("evidence_ids", []) or []),
                citation_ids=list(previous.get("citation_ids", []) or []),
                open_gaps=[f"等待上游模块完成：{', '.join(blocked_deps)}"],
                assumptions=list(previous.get("assumptions", []) or []),
                last_worker_role=str(previous.get("last_worker_role", "") or "argument_worker"),
                last_review_score=float(previous.get("last_review_score", 0.0) or 0.0),
                locked=bool(previous.get("locked")),
            ).model_dump()
            status[module_id] = "skipped"
            changed = True

    blocked_modules = [
        {
            "module_id": module_id,
            "reason": "; ".join(outputs.get(module_id, {}).get("open_gaps", []) or []),
        }
        for module_id, module_status_value in status.items()
        if module_status_value in {"blocked", "skipped"}
    ]

    return {
        "module_outputs": outputs,
        "module_status": status,
        "evidence_bank": evidence_bank,
        "citation_bank": build_citation_bank(evidence_bank),
        "worker_results": worker_results,
        "blocked_modules": blocked_modules,
    }
