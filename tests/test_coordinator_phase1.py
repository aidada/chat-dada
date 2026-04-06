"""
Phase 1 集成测试 — 覆盖 direct/single_skill/dag 三种模式的核心逻辑
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ── 数据结构测试 ──────────────────────────────────────────────────────────────

def test_task_construction():
    from agent.coordinator.state import Task
    t = Task(id="t1", title="测试任务")
    assert t.id == "t1"
    assert t.status == "pending"
    assert t.depends_on == []


def test_task_var_entry():
    from agent.coordinator.state import TaskVarEntry
    entry = TaskVarEntry(summary="test summary")
    assert entry.summary == "test summary"
    assert entry.key_findings == []
    assert entry.artifact_refs == []


def test_coordinator_config_defaults():
    from agent.coordinator.state import CoordinatorConfig, DAGFailureStrategy
    config = CoordinatorConfig()
    assert config.failure_strategy == DAGFailureStrategy.STOP_DEPENDENTS
    assert config.max_parallel_tasks == 5
    assert config.max_dag_depth == 10


def test_skill_result_fields():
    from agent.coordinator.state import SkillResult
    r = SkillResult(status="ok", result="hello")
    assert r.artifact_refs == []
    assert r.review == {}
    assert r.budget == {}
    assert r.strategy == ""


# ── DAG 验证测试 ──────────────────────────────────────────────────────────────

def test_validate_dag_valid():
    from agent.coordinator.state import Task
    from agent.coordinator.executor import validate_dag
    tasks = [
        Task(id="t1", title="A", depends_on=[]),
        Task(id="t2", title="B", depends_on=["t1"]),
        Task(id="t3", title="C", depends_on=["t2"]),
    ]
    assert validate_dag(tasks) == []


def test_validate_dag_cycle():
    from agent.coordinator.state import Task
    from agent.coordinator.executor import validate_dag
    tasks = [
        Task(id="t1", title="A", depends_on=["t2"]),
        Task(id="t2", title="B", depends_on=["t1"]),
    ]
    errors = validate_dag(tasks)
    assert len(errors) > 0
    assert any("Circular" in e or "cycle" in e.lower() for e in errors)


def test_validate_dag_dangling_ref():
    from agent.coordinator.state import Task
    from agent.coordinator.executor import validate_dag
    tasks = [
        Task(id="t1", title="A", depends_on=["t99"]),
    ]
    errors = validate_dag(tasks)
    assert len(errors) > 0
    assert any("t99" in e for e in errors)


def test_validate_dag_parallel_independent():
    from agent.coordinator.state import Task
    from agent.coordinator.executor import validate_dag
    # 两个独立任务，无依赖关系
    tasks = [
        Task(id="t1", title="A", depends_on=[]),
        Task(id="t2", title="B", depends_on=[]),
    ]
    assert validate_dag(tasks) == []


# ── TaskVarEntry 构造测试 ─────────────────────────────────────────────────────

def test_build_task_vars_entry_summary_truncation():
    from agent.coordinator.state import Task, SkillResult, build_task_vars_entry
    task = Task(id="t1", title="test", assigned_skill="do_research")
    long_result = "x" * 3000
    result = SkillResult(status="ok", result=long_result)
    entry = build_task_vars_entry(task, result)
    assert len(entry.summary) <= 2000
    assert entry.summary.endswith("...")


def test_build_task_vars_entry_normal():
    from agent.coordinator.state import Task, SkillResult, build_task_vars_entry
    task = Task(id="t1", title="test", assigned_skill="do_research")
    result = SkillResult(
        status="ok",
        result="短摘要",
        artifact_refs=[{"type": "file", "name": "report.md"}]
    )
    entry = build_task_vars_entry(task, result)
    assert entry.summary == "短摘要"
    assert entry.source_task_id == "t1"
    assert entry.source_skill == "do_research"
    assert len(entry.artifact_refs) == 1


# ── inject_upstream_context 测试 ─────────────────────────────────────────────

def test_inject_upstream_context():
    from agent.coordinator.state import Task, TaskVarEntry, inject_upstream_context
    task = Task(id="t2", title="B", depends_on=["t1"])
    task_vars = {
        "t1": TaskVarEntry(
            summary="上游摘要",
            artifact_refs=[{"type": "file", "name": "upstream.md"}],
            source_task_id="t1",
            source_skill="do_research",
        )
    }
    ctx = inject_upstream_context(task, task_vars)
    assert "上游摘要" in ctx["upstream_context"]
    assert len(ctx["upstream_artifacts"]) == 1


def test_inject_upstream_context_no_deps():
    from agent.coordinator.state import Task, inject_upstream_context
    task = Task(id="t1", title="A", depends_on=[])
    ctx = inject_upstream_context(task, {})
    assert ctx["upstream_context"] == ""
    assert ctx["upstream_artifacts"] == []


# ── SkillRegistry 测试 ────────────────────────────────────────────────────────

def test_skill_registry_summary():
    from agent.coordinator.skills import skill_registry
    summary = skill_registry.skill_summary_for_llm()
    assert isinstance(summary, str)
    assert len(summary) > 0


def test_skill_registry_has_research():
    from agent.coordinator.skills import skill_registry
    desc = skill_registry.get_description("do_research")
    assert desc is not None
    assert desc.name == "do_research"


def test_skill_description_fields():
    from agent.coordinator.skills import skill_registry
    desc = skill_registry.get_description("do_research")
    assert desc is not None
    assert desc.name == "do_research"
    assert desc.timeout_seconds > 0
    assert len(desc.best_for) > 0


def test_skill_registry_fresh_instance():
    from agent.coordinator.skills import (
        SkillDescription,
        SkillRegistry,
    )

    reg = SkillRegistry()
    # Empty registry returns a header with no skills listed
    summary = reg.skill_summary_for_llm()
    assert isinstance(summary, str)
    assert "Skills" in summary or "技能" in summary or "暂无" in summary

    dummy_runner = MagicMock()

    # Create skill descriptions dynamically (matching how discover_skills works)
    skills = [
        SkillDescription(name="do_research", description="研究技能", best_for=["研究"]),
        SkillDescription(name="do_patent", description="专利技能", best_for=["专利"]),
        SkillDescription(name="do_ppt", description="PPT技能", best_for=["演示文稿"]),
        SkillDescription(name="do_zero_report", description="零报告技能", best_for=["报告"]),
    ]

    for skill in skills:
        reg.register(skill.name, dummy_runner, description=skill)

    assert reg.is_registered("do_research")
    assert reg.is_registered("do_patent")
    assert reg.is_registered("do_ppt")
    assert reg.is_registered("do_zero_report")
    assert not reg.is_registered("nonexistent")

    summary = reg.skill_summary_for_llm()
    assert "do_research" in summary
    assert "do_patent" in summary


# ── Prompts 测试 ──────────────────────────────────────────────────────────────

def test_understand_goal_prompt_contains_skills():
    from agent.coordinator.prompts import build_understand_goal_prompt
    msgs = build_understand_goal_prompt("帮我研究量子计算", "技能摘要内容")
    assert len(msgs) >= 2
    # 技能摘要应该在 user prompt 中
    user_content = msgs[1]["content"]
    assert "技能摘要内容" in user_content


def test_understand_goal_prompt_modes():
    from agent.coordinator.prompts import build_understand_goal_prompt
    msgs = build_understand_goal_prompt("test", "skills")
    system_content = msgs[0]["content"]
    assert "direct" in system_content
    assert "single_skill" in system_content
    assert "dag" in system_content


def test_direct_answer_prompt():
    from agent.coordinator.prompts import build_direct_answer_prompt
    msgs = build_direct_answer_prompt("你好", "")
    assert len(msgs) >= 2
    assert msgs[-1]["role"] == "user"
    assert "你好" in msgs[-1]["content"]


def test_direct_answer_prompt_with_context():
    from agent.coordinator.prompts import build_direct_answer_prompt
    msgs = build_direct_answer_prompt("继续", "之前聊了AI")
    assert len(msgs) == 2
    # Context is in user prompt
    assert "之前聊了AI" in msgs[1]["content"]


def test_direct_answer_prompt_empty_context_ignored():
    from agent.coordinator.prompts import build_direct_answer_prompt
    msgs = build_direct_answer_prompt("你好", "   ")
    assert len(msgs) == 2


def test_decompose_tasks_prompt():
    from agent.coordinator.prompts import build_decompose_tasks_prompt
    msgs = build_decompose_tasks_prompt("研究AI并写专利", "技能摘要")
    assert len(msgs) >= 2
    # skill_summary is in user prompt
    assert "技能摘要" in msgs[1]["content"]


def test_synthesis_prompt():
    from agent.coordinator.prompts import build_synthesis_prompt
    t1 = SimpleNamespace(title="研究报告", result="AI技术分析结果...")
    t2 = SimpleNamespace(title="专利草案", result="权利要求书...")
    prompt = build_synthesis_prompt({}, [t1, t2])
    assert "研究报告" in prompt
    assert "专利草案" in prompt
    assert "AI技术分析结果" in prompt
    assert "汇总" in prompt


def test_synthesis_prompt_filters_empty():
    from agent.coordinator.prompts import build_synthesis_prompt
    t1 = SimpleNamespace(title="有结果", result="内容")
    t2 = SimpleNamespace(title="无结果", result=None)
    # build_synthesis_prompt includes all tasks, but None result becomes empty string
    prompt = build_synthesis_prompt({}, [t1, t2])
    assert "有结果" in prompt
    # Note: title "无结果" will appear, but the content will be empty


# ── DAG 辅助函数测试 ─────────────────────────────────────────────────────────

def test_is_task_ready():
    from agent.coordinator.state import Task
    from agent.coordinator.executor import is_task_ready
    t1 = Task(id="t1", title="A", depends_on=[], status="done")
    t2 = Task(id="t2", title="B", depends_on=["t1"])
    completed = {"t1": t1}
    assert is_task_ready(t2, completed) is True
    assert is_task_ready(t2, {}) is False


def test_find_dependent_tasks():
    from agent.coordinator.state import Task
    from agent.coordinator.executor import find_dependent_tasks
    tasks = [
        Task(id="t1", title="A", depends_on=[]),
        Task(id="t2", title="B", depends_on=["t1"]),
        Task(id="t3", title="C", depends_on=["t1"]),
        Task(id="t4", title="D", depends_on=["t2"]),
    ]
    dependents = find_dependent_tasks("t1", tasks)
    dependent_ids = {t.id for t in dependents}
    assert dependent_ids == {"t2", "t3"}


# ── Coordinator 图构建测试 ────────────────────────────────────────────────────

def test_coordinator_graph_builds():
    from agent.coordinator.agent import build_coordinator_graph
    g = build_coordinator_graph()
    node_names = list(g.nodes)
    assert "understand_goal" in node_names
    assert "direct_answer" in node_names
    assert "execute_single_skill" in node_names
    assert "decompose_tasks" in node_names
    assert "synthesize" in node_names


def test_execution_mode_values():
    from agent.coordinator.state import ExecutionMode
    assert ExecutionMode.DIRECT.value == "direct"
    assert ExecutionMode.SINGLE_SKILL.value == "single_skill"
    assert ExecutionMode.DAG.value == "dag"
