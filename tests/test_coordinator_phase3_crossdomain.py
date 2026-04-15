"""
Phase 3 End-to-End Cross-Domain Tests
======================================
Executes 10+ cross-domain sample tasks through the Coordinator DAG mode,
collecting success rate, latency, cost, and structured field metrics.

Run with: pytest tests/test_coordinator_phase3_crossdomain.py -v
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.coordinator.state import (
    CoordinatorConfig,
    CoordinatorState,
    ExecutionMode,
    SkillResult,
)


# ── Fixed Cross-Domain Samples ─────────────────────────────────────────────────

CROSS_DOMAIN_SAMPLES = [
    {
        "id": "cd01",
        "goal": "研究量子计算最新进展，并基于研究结果撰写专利申请",
        "expected_skills": ["do_research", "do_patent"],
        "expected_mode": "dag",
    },
    {
        "id": "cd02",
        "goal": "调研竞品技术方案，生成PPT演示文稿并附上分析报告",
        "expected_skills": ["do_research", "do_office"],
        "expected_mode": "dag",
    },
    {
        "id": "cd03",
        "goal": "分析最新AI论文，生成深度研究报告",
        "expected_skills": ["do_research"],
        "expected_mode": "single_skill",
    },
    {
        "id": "cd04",
        "goal": "调研氢能源技术发展现状，写一份完整的技术专利布局分析",
        "expected_skills": ["do_research", "do_patent"],
        "expected_mode": "dag",
    },
    {
        "id": "cd05",
        "goal": "帮我制作一个介绍公司新产品的PPT，包含产品功能演示和竞争优势分析",
        "expected_skills": ["do_office"],
        "expected_mode": "single_skill",
    },
    {
        "id": "cd06",
        "goal": "分析近期数据中心故障事件，生成零报告（事故分析报告）",
        "expected_skills": ["do_zero_report"],
        "expected_mode": "single_skill",
    },
    {
        "id": "cd07",
        "goal": "研究脑机接口技术最新进展，生成综述报告，并制作配套PPT",
        "expected_skills": ["do_research", "do_office"],
        "expected_mode": "dag",
    },
    {
        "id": "cd08",
        "goal": "调研竞争对手的AI产品功能，生成对比分析报告和专利侵权分析",
        "expected_skills": ["do_research", "do_patent"],
        "expected_mode": "dag",
    },
    {
        "id": "cd09",
        "goal": "帮我写一个关于区块链在供应链应用的技术专利",
        "expected_skills": ["do_patent"],
        "expected_mode": "single_skill",
    },
    {
        "id": "cd10",
        "goal": "分析电动汽车续航技术突破，生成行业研究报告，并制作CEO汇报PPT",
        "expected_skills": ["do_research", "do_office"],
        "expected_mode": "dag",
    },
    {
        "id": "cd11",
        "goal": "调研量子机器学习的研究现状和商业化前景，生成完整研究报告",
        "expected_skills": ["do_research"],
        "expected_mode": "single_skill",
    },
    {
        "id": "cd12",
        "goal": "对最近的网络安全事件进行根因分析，生成零报告和改进建议",
        "expected_skills": ["do_zero_report"],
        "expected_mode": "single_skill",
    },
]


# ── Mock Skill Adapter ────────────────────────────────────────────────────────

SKILL_MOCK_RESULTS = {
    "do_research": SkillResult(
        status="ok",
        result="研究结果表明该领域技术快速发展，已有多个突破性进展。",
        artifact_refs=[{"name": "research_report.md", "type": "file"}],
        review={"quality_score": 0.85},
        budget={"cost_usd": 2.5},
    ),
    "do_patent": SkillResult(
        status="ok",
        result="专利申请文件已起草完成，包括权利要求书和说明书。",
        artifact_refs=[{"name": "patent_application.pdf", "type": "file"}],
        review={"completeness": 0.9},
        budget={"cost_usd": 1.8},
    ),
    "do_office": SkillResult(
        status="ok",
        result="PPT演示文稿已生成，共15页幻灯片。",
        artifact_refs=[{"name": "presentation.pptx", "type": "file"}],
        review={"design_score": 0.88},
        budget={"cost_usd": 1.2},
    ),
    "do_zero_report": SkillResult(
        status="ok",
        result="零报告已完成，包含时间线、根因分析和整改建议。",
        artifact_refs=[{"name": "incident_report.md", "type": "file"}],
        review={"accuracy": 0.92},
        budget={"cost_usd": 1.5},
    ),
}


async def mock_skill_adapter_research_patent(runner: Any, input_data: dict, context: Any) -> SkillResult:
    """Mock adapter: research → patent with upstream context."""
    skill = context.skill_name
    result = SKILL_MOCK_RESULTS.get(skill)
    if result:
        return result
    raise RuntimeError(f"Unknown skill: {skill}")


# ── LLM Response Builders ────────────────────────────────────────────────────

def build_understand_goal_response(sample: dict) -> dict:
    """Build mock understand_goal LLM response based on expected mode."""
    mode = sample.get("expected_mode", "dag")
    if mode == "single_skill":
        skill = sample["expected_skills"][0] if sample["expected_skills"] else "do_research"
        return {
            "execution_mode": "single_skill",
            "reasoning": f"单一技能任务，适合{skill}",
            "goal_understanding": sample["goal"],
            "selected_skill": skill,
            "skill_input": {"query": sample["goal"]},
        }
    else:
        # DAG mode - cross-domain
        tasks = []
        skills = sample.get("expected_skills", [])
        if len(skills) == 1:
            tasks.append({
                "id": "t1",
                "title": f"执行{skills[0]}",
                "description": sample["goal"],
                "depends_on": [],
                "assigned_skill": skills[0],
                "input_data": {"query": sample["goal"]},
            })
        elif len(skills) == 2:
            tasks.append({
                "id": "t1",
                "title": f"执行{skills[0]}",
                "description": f"第一阶段：{sample['goal']}",
                "depends_on": [],
                "assigned_skill": skills[0],
                "input_data": {"query": sample["goal"]},
            })
            tasks.append({
                "id": "t2",
                "title": f"执行{skills[1]}",
                "description": f"第二阶段：基于{skills[0]}结果",
                "depends_on": ["t1"],
                "assigned_skill": skills[1],
                "input_data": {"query": f"基于研究结果的{skills[1]}"},
            })
        return {
            "execution_mode": "dag",
            "reasoning": "跨领域任务，需要多技能协作",
            "goal_understanding": sample["goal"],
            "tasks": tasks,
        }


def build_decompose_response(sample: dict) -> dict:
    """Build mock decompose_tasks LLM response."""
    skills = sample.get("expected_skills", [])
    if len(skills) == 1:
        return {
            "tasks": [{
                "id": "t1",
                "title": f"执行{skills[0]}",
                "description": sample["goal"],
                "depends_on": [],
                "assigned_skill": skills[0],
                "input_data": {"query": sample["goal"]},
            }]
        }
    elif len(skills) == 2:
        return {
            "tasks": [
                {
                    "id": "t1",
                    "title": f"执行{skills[0]}",
                    "description": f"第一阶段",
                    "depends_on": [],
                    "assigned_skill": skills[0],
                    "input_data": {"query": sample["goal"]},
                },
                {
                    "id": "t2",
                    "title": f"执行{skills[1]}",
                    "description": f"第二阶段",
                    "depends_on": ["t1"],
                    "assigned_skill": skills[1],
                    "input_data": {"query": f"基于{skills[0]}结果"},
                },
            ]
        }
    return {"tasks": []}


# ── Graph Builder ─────────────────────────────────────────────────────────────

def _build_graph():
    from agent.coordinator.agent import build_coordinator_graph
    return build_coordinator_graph()


# ── Test: Execute Single Cross-Domain Sample ──────────────────────────────────

@pytest.mark.asyncio
async def test_crossdomain_sample_cd01():
    """CD01: 研究量子计算最新进展，并基于研究结果撰写专利申请"""
    sample = next(s for s in CROSS_DOMAIN_SAMPLES if s["id"] == "cd01")

    goal_response = build_understand_goal_response(sample)
    decompose_response = build_decompose_response(sample)

    call_count = [0]
    def llm_factory(role: str, **kwargs: Any):
        llm = AsyncMock()
        if call_count[0] == 0:
            llm.ainvoke.return_value = _make_llm_response(goal_response)
        else:
            llm.ainvoke.return_value = _make_llm_response(decompose_response)
        call_count[0] += 1
        return llm

    mock_runner = MagicMock()
    graph = _build_graph()

    with patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.skills.run_skill_via_adapter",
               side_effect=mock_skill_adapter_research_patent), \
         patch("agent.coordinator.agent.get_stream_writer",
               return_value=lambda _: None), \
         patch("agent.coordinator.executor.get_stream_writer",
               return_value=lambda _: None), \
         patch("core.models.get_llm", side_effect=llm_factory):

        mock_reg.skill_summary_for_llm.return_value = "技能摘要"
        mock_reg.list_skills.return_value = []
        mock_reg.is_registered.return_value = True
        mock_reg.get_runner.return_value = mock_runner

        config = {"configurable": {"thread_id": f"test-{sample['id']}"}}
        state_input = {
            "original_goal": sample["goal"],
            "trace_id": f"trace-{sample['id']}",
            "config": CoordinatorConfig(),
            "clarification_history": [],
        }

        start = time.perf_counter()
        result = await graph.ainvoke(state_input, config=config)
        elapsed = time.perf_counter() - start

    # Assertions
    mode = result.get("execution_mode")
    assert mode == ExecutionMode.DAG, f"Expected DAG mode, got {mode}"

    completed = result.get("completed_tasks") or {}
    assert len(completed) >= 1, f"Expected at least 1 completed task, got {len(completed)}"

    artifact_refs = result.get("artifact_refs") or []
    assert len(artifact_refs) > 0, "artifact_refs should not be empty"

    review = result.get("review") or {}
    assert len(review) > 0, "review should not be empty"

    budget = result.get("budget") or {}
    assert len(budget) > 0, "budget should not be empty"

    strategy_trace = result.get("strategy_trace") or []
    assert len(strategy_trace) > 0, "strategy_trace should not be empty"

    final_result = result.get("final_result") or ""
    assert len(final_result) > 0, "final_result should not be empty"

    print(f"\n[CD01] elapsed={elapsed:.2f}s, completed_tasks={len(completed)}, "
          f"artifact_refs={len(artifact_refs)}, review_fields={len(review)}, "
          f"budget_fields={len(budget)}, strategy_trace={strategy_trace}")


# ── Parametrized Cross-Domain Tests ──────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("sample", CROSS_DOMAIN_SAMPLES, ids=lambda s: s["id"])
async def test_crossdomain_all_samples(sample: dict):
    """
    Execute all 12 cross-domain samples and collect metrics.
    This is the primary data collection test for Phase 3 G1/G5 validation.
    """
    goal_response = build_understand_goal_response(sample)
    decompose_response = build_decompose_response(sample)

    call_count = [0]
    def llm_factory(role: str, **kwargs: Any):
        llm = AsyncMock()
        if call_count[0] == 0:
            llm.ainvoke.return_value = _make_llm_response(goal_response)
        else:
            llm.ainvoke.return_value = _make_llm_response(decompose_response)
        call_count[0] += 1
        return llm

    def skill_adapter_factory(runner: Any, input_data: dict, context: Any) -> SkillResult:
        skill = context.skill_name
        result = SKILL_MOCK_RESULTS.get(skill)
        if result:
            return result
        # Default fallback
        return SkillResult(
            status="ok",
            result=f"{skill}执行完成",
            artifact_refs=[{"name": f"{skill}_output.md", "type": "file"}],
            review={},
            budget={},
        )

    mock_runner = MagicMock()
    graph = _build_graph()

    with patch("agent.coordinator.skills.skill_registry") as mock_reg, \
         patch("agent.coordinator.skills.run_skill_via_adapter",
               side_effect=skill_adapter_factory), \
         patch("agent.coordinator.agent.get_stream_writer",
               return_value=lambda _: None), \
         patch("agent.coordinator.executor.get_stream_writer",
               return_value=lambda _: None), \
         patch("core.models.get_llm", side_effect=llm_factory):

        mock_reg.skill_summary_for_llm.return_value = "技能摘要"
        mock_reg.list_skills.return_value = []
        mock_reg.is_registered.side_effect = lambda name: name in SKILL_MOCK_RESULTS
        mock_reg.get_runner.return_value = mock_runner

        config = {"configurable": {"thread_id": f"test-{sample['id']}"}}
        state_input = {
            "original_goal": sample["goal"],
            "trace_id": f"trace-{sample['id']}",
            "config": CoordinatorConfig(),
            "clarification_history": [],
        }

        start = time.perf_counter()
        try:
            result = await graph.ainvoke(state_input, config=config)
            elapsed = time.perf_counter() - start
            success = True
            error_msg = ""
        except Exception as exc:
            elapsed = time.perf_counter() - start
            success = False
            error_msg = str(exc)
            result = {}

    mode = result.get("execution_mode") if success else None
    completed = result.get("completed_tasks") or {}
    failed = result.get("failed_tasks") or {}
    artifact_refs = result.get("artifact_refs") or []
    review = result.get("review") or {}
    budget = result.get("budget") or {}
    strategy_trace = result.get("strategy_trace") or []
    final_result = result.get("final_result") or ""

    # Estimate cost from budget
    cost_usd = 0.0
    if budget.get("tasks"):
        for task_budget in budget["tasks"].values():
            cost_usd += float(task_budget.get("cost_usd", 0))
    elif budget.get("cost_usd"):
        cost_usd = float(budget.get("cost_usd", 0))

    print(f"\n[{sample['id']}] "
          f"success={success} "
          f"mode={mode} "
          f"elapsed={elapsed:.2f}s "
          f"cost=${cost_usd:.2f} "
          f"completed={len(completed)} "
          f"failed={len(failed)} "
          f"artifact_refs={len(artifact_refs)} "
          f"has_review=({len(review)>0}) "
          f"has_budget=({len(budget)>0}) "
          f"has_trace=({len(strategy_trace)>0}) "
          f"has_result=({len(final_result)>0}) "
          f"error={error_msg[:50] if error_msg else 'none'}")

    # G1: Success = no failed tasks AND has final_result
    is_successful = success and len(failed) == 0 and len(final_result) > 0
    assert is_successful, f"Sample {sample['id']} failed: {error_msg}"


# ── Helper ─────────────────────────────────────────────────────────────────────

def _make_llm_response(payload: dict) -> Any:
    mock_response = MagicMock()
    mock_response.text = None
    mock_response.content = json.dumps(payload)
    return mock_response
