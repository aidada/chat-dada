"""科研领域正式入口。

这里只负责两件事：
1. 调用新的科研工作流图执行任务；
2. 把工作流中间产物和最终结果持久化到 `ResearchMemory`。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from capabilities.memory import ResearchMemory
from domain_agents.research.schemas import ResearchDomainResult
from domain_agents.research.utils import (
    build_evidence_and_citations,
    collect_artifact_refs,
    fallback_brief,
    feedback_action,
    persist_evidence_and_citations,
    strategy_summary,
)
from domain_agents.research.workflow import (
    CHECKPOINT_C_PROMPT,
    build_research_workflow_graph,
    synthesize_final_payload,
)
from task_platform.streaming import stream_nested_graph

_log = logging.getLogger("chatdada.research.orchestrated")
_graph = build_research_workflow_graph()


def _read_json_file(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _log.warning("Failed to read JSON file: %s", path, exc_info=True)
        return None


def _latest_nested_clarification(input_data: dict[str, Any]) -> dict[str, Any] | None:
    clarification_history = list(input_data.get("clarification_history", []) or [])
    for item in reversed(clarification_history):
        if not isinstance(item, dict):
            continue
        if str(item.get("nested_graph", "") or "").strip():
            return item
    return None


def _is_checkpoint_c_accept(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict):
        return False
    answer = str(entry.get("answer", "") or "").strip()
    if feedback_action(answer) != "accept":
        return False

    graph_node = str(entry.get("graph_node", "") or "").strip()
    if graph_node.endswith("checkpoint_c"):
        return True

    question = str(entry.get("question", "") or "").strip()
    return question == CHECKPOINT_C_PROMPT


async def _resume_from_checkpoint_c_accept(
    *,
    task_id: str,
    query: str,
    report_profile: str,
    input_data: dict[str, Any],
) -> dict[str, Any] | None:
    latest_entry = _latest_nested_clarification(input_data)
    if not _is_checkpoint_c_accept(latest_entry):
        return None

    memory = ResearchMemory(task_id)
    aggregated_path = memory.task_dir / "aggregated_draft.md"
    aggregated_draft = aggregated_path.read_text(encoding="utf-8").strip() if aggregated_path.exists() else ""
    if not aggregated_draft:
        _log.warning(
            "Checkpoint C accept fast-forward skipped: missing aggregated_draft task_id=%s",
            task_id,
        )
        return None

    latest_checkpoint = memory.load_checkpoint() or {}
    module_outputs = latest_checkpoint.get("module_outputs")
    if not isinstance(module_outputs, dict):
        module_outputs = _read_json_file(memory.task_dir / "module_outputs.json")
    if not isinstance(module_outputs, dict):
        module_outputs = {}

    evaluation = latest_checkpoint.get("evaluation")
    if not isinstance(evaluation, dict):
        evaluation = _read_json_file(memory.task_dir / "evaluation.json")
    if not isinstance(evaluation, dict):
        evaluation = {}

    blocked_modules = latest_checkpoint.get("blocked_modules")
    if not isinstance(blocked_modules, list):
        blocked_modules = []

    budget = latest_checkpoint.get("budget")
    if not isinstance(budget, dict):
        budget = _read_json_file(memory.task_dir / "budget.json")
    if not isinstance(budget, dict):
        budget = {}

    clarification_history = list(input_data.get("clarification_history", []) or [])
    if clarification_history and clarification_history[-1] is latest_entry:
        clarification_history = clarification_history[:-1]
    elif latest_entry in clarification_history:
        clarification_history = [item for item in clarification_history if item is not latest_entry]

    brief = fallback_brief(
        query,
        report_profile,
        {**input_data, "clarification_history": clarification_history},
    )
    resume_state = {
        "task_id": task_id,
        "query": query,
        "report_profile": report_profile,
        "brief": brief,
        "module_outputs": module_outputs,
        "evaluations": [evaluation] if evaluation else [],
        "aggregated_draft": aggregated_draft,
        "blocked_modules": blocked_modules,
        "budget": budget,
        "workflow_trace": ["checkpoint_c_accept_resume"],
    }
    _log.info(
        "Fast-forwarding checkpoint_c accept to final synthesis: task_id=%s question=%s",
        task_id,
        str(latest_entry.get("question", "") or "")[:120],
    )
    return {**resume_state, **(await synthesize_final_payload(resume_state))}


def _normalize_artifact_refs(task_id: str, task_dir: Path, refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for ref in refs:
        raw_path = str(ref.get("path", "") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path)
        try:
            relative_path = str(path.relative_to(task_dir))
        except ValueError:
            relative_path = path.name
        normalized.append(
            {
                **ref,
                "path": relative_path,
                "url": f"/tasks/{task_id}/artifact-file?path={quote(relative_path, safe='')}",
            }
        )
    return normalized


def _persist_workflow_artifacts(task_id: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """把计划、模块草案、评估结果和最终报告统一落盘。"""

    memory = ResearchMemory(task_id)
    query = str(result.get("query", "") or "")
    report_profile = str(result.get("report_profile", "") or "")
    if memory.load_meta() is None:
        memory.init(query, report_profile)

    task_dir = memory.task_dir
    task_dir.mkdir(parents=True, exist_ok=True)

    final_report = str(result.get("final_result", "") or "")
    if final_report:
        memory.save_final_report(final_report)

    aggregated_draft = str(result.get("aggregated_draft", "") or "")
    if aggregated_draft:
        (task_dir / "aggregated_draft.md").write_text(aggregated_draft, encoding="utf-8")

    plan = result.get("plan") or {}
    if plan:
        (task_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    module_outputs = result.get("module_outputs") or {}
    if module_outputs:
        (task_dir / "module_outputs.json").write_text(
            json.dumps(module_outputs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    evaluations = result.get("evaluations") or []
    if evaluations:
        (task_dir / "evaluation.json").write_text(
            json.dumps(evaluations[-1], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (task_dir / "evaluations.json").write_text(
            json.dumps(evaluations, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    draft_history = result.get("draft_history") or []
    if draft_history:
        (task_dir / "draft_history.json").write_text(
            json.dumps(draft_history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    last_evaluation_diff = result.get("last_evaluation_diff") or {}
    if last_evaluation_diff:
        (task_dir / "last_evaluation_diff.json").write_text(
            json.dumps(last_evaluation_diff, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    budget = result.get("budget") or {}
    if budget:
        (task_dir / "budget.json").write_text(
            json.dumps(budget, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    evidence, citations = build_evidence_and_citations(
        task_id,
        final_report,
        module_outputs=module_outputs,
    )
    refs = collect_artifact_refs(task_dir)
    refs.extend(persist_evidence_and_citations(task_dir, evidence, citations))
    return _normalize_artifact_refs(task_id, task_dir, refs)


async def run_research_domain_orchestrated(input_data: dict[str, Any]) -> ResearchDomainResult:
    """运行科研工作流，并返回统一的领域结果对象。"""

    query = str(input_data.get("query", input_data.get("task", "")) or "").strip()
    task_id = str(input_data.get("task_id", "") or "research_preview")
    report_profile = str(input_data.get("report_profile", "") or "")

    _log.info("Starting research workflow: query=%s task_id=%s", query[:80], task_id)

    resumed_result = await _resume_from_checkpoint_c_accept(
        task_id=task_id,
        query=query,
        report_profile=report_profile,
        input_data=input_data,
    )
    if resumed_result is not None:
        result = resumed_result
    else:
        result = await stream_nested_graph(
            _graph,
            {
                "query": query,
                "task_id": task_id,
                "report_profile": report_profile,
                "input_payload": dict(input_data),
                "module_outputs": {},
                "module_status": {},
                "evidence_bank": [],
                "citation_bank": [],
                "evaluations": [],
                "revision_targets": [],
                "locked_modules": {},
                "blocked_modules": [],
                "active_modules": [],
                "last_evaluation_diff": {},
                "budget": {},
                "draft_history": [],
                "feedback_history": [],
                "workflow_trace": [],
                "revision_round": 0,
                "progress": 0.0,
                "cost": 0.0,
            },
            config={"configurable": {"thread_id": task_id}},
            extra_payload={
                "nested_graph": "research_workflow",
                "domain_name": "research",
                "source": "research_workflow",
            },
        )

    final_text = str(result.get("final_result", "") or result.get("aggregated_draft", "") or "")
    latest_review = dict((result.get("evaluations") or [{}])[-1] or {})
    latest_review.update(
        {
            "revision_round": int(result.get("revision_round", 0) or 0),
            "active_modules": list(result.get("active_modules", []) or []),
            "blocked_modules": list(result.get("blocked_modules", []) or []),
            "last_evaluation_diff": dict(result.get("last_evaluation_diff", {}) or {}),
        }
    )
    artifact_refs = _persist_workflow_artifacts(task_id, {**result, "query": query, "report_profile": report_profile})
    workflow_trace = [str(item) for item in result.get("workflow_trace", []) if str(item).strip()]
    # 极少数旧测试仍会 mock `step_history`，这里做一次兜底兼容，
    # 但真实逻辑已经全部迁移到 `workflow_trace`。
    if not workflow_trace and result.get("step_history"):
        workflow_trace = [
            str(item.get("strategy", "") or "").strip()
            for item in result.get("step_history", [])
            if str(item.get("strategy", "") or "").strip()
        ]

    return ResearchDomainResult(
        status="ok",
        result=final_text or "研究工作流未生成最终结果。",
        artifact_refs=artifact_refs,
        review=latest_review,
        budget=dict(result.get("budget", {}) or {}),
        strategy=strategy_summary(workflow_trace),
    )
