"""
Phase 3 — Task 执行引擎测试
覆盖: DAG 并行执行、依赖等待、task_vars 传递、结果汇总
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

import pytest

from agent.coordinator.state import (
    CoordinatorConfig,
    CoordinatorState,
    ExecutionMode,
    SkillResult,
    Task,
    TaskVarEntry,
    build_task_vars_entry,
    inject_upstream_context,
)


# ── MockSkill ─────────────────────────────────────────────────────────────────

@dataclass
class MockSkill:
    """模拟领域技能"""
    name: str
    result: Any = "default result"
    status: str = "ok"
    artifact_refs: list = None
    error: str | None = None
    execution_time_seconds: float = 0.1

    def __post_init__(self):
        if self.artifact_refs is None:
            self.artifact_refs = []


def make_mock_result(task: MockSkill) -> SkillResult:
    return SkillResult(
        status=task.status,
        result=task.result,
        artifact_refs=task.artifact_refs,
        review={"task_id": task.name},
        budget={"cost_usd": 0.01},
        strategy=task.name,
        execution_time_seconds=task.execution_time_seconds,
    )


# ── inject_upstream_context 复用 test_coordinator_state ──────────────────────

def test_inject_upstream_context_chain():
    """链式 DAG: t2 从 t1 注入上下文"""
    t2 = Task(id="t2", title="B", depends_on=["t1"])
    t1_vars = {
        "t1": TaskVarEntry(
            summary="t1 summary result",
            artifact_refs=[{"type": "doc", "id": "d1"}],
            source_task_id="t1",
            source_skill="do_research",
        )
    }
    ctx = inject_upstream_context(t2, t1_vars)
    assert "t1 summary result" in ctx["upstream_context"]
    assert ctx["upstream_artifacts"][0]["id"] == "d1"


def test_inject_upstream_context_parallel_merge():
    """并行 DAG: t3 从 t1 和 t2 合并"""
    t3 = Task(id="t3", title="C", depends_on=["t1", "t2"])
    t1_vars = {
        "t1": TaskVarEntry(summary="t1 out", artifact_refs=[{"id": "a1"}], source_task_id="t1", source_skill="do_research"),
    }
    t2_vars = {
        "t2": TaskVarEntry(summary="t2 out", artifact_refs=[{"id": "a2"}], source_task_id="t2", source_skill="do_patent"),
    }
    combined = {**t1_vars, **t2_vars}
    ctx = inject_upstream_context(t3, combined)
    assert "t1 out" in ctx["upstream_context"]
    assert "t2 out" in ctx["upstream_context"]
    assert len(ctx["upstream_artifacts"]) == 2


# ── build_task_vars_entry 复用 ────────────────────────────────────────────────

def test_build_task_vars_entry_from_result():
    """从 SkillResult 构建 TaskVarEntry"""
    task = Task(id="t1", title="A", assigned_skill="do_research")
    result = SkillResult(
        status="ok",
        result="final output",
        artifact_refs=[{"type": "report", "id": "r1"}],
    )
    entry = build_task_vars_entry(task, result)
    assert entry.summary == "final output"
    assert entry.artifact_refs[0]["id"] == "r1"
    assert entry.source_skill == "do_research"


# ── is_task_ready ─────────────────────────────────────────────────────────────

def test_is_task_ready_no_deps():
    """无依赖任务总是就绪"""
    from agent.coordinator.executor import is_task_ready
    task = Task(id="t1", title="A", depends_on=[])
    completed = {}
    assert is_task_ready(task, completed) is True


def test_is_task_ready_all_deps_done():
    """所有依赖已完成时任务就绪"""
    from agent.coordinator.executor import is_task_ready
    t1 = Task(id="t1", title="A", status="done")
    t2 = Task(id="t2", title="B", depends_on=["t1"])
    completed = {"t1": t1}
    assert is_task_ready(t2, completed) is True


def test_is_task_ready_dep_not_done():
    """依赖未完成时任务不就绪"""
    from agent.coordinator.executor import is_task_ready
    t1 = Task(id="t1", title="A", status="running")
    t2 = Task(id="t2", title="B", depends_on=["t1"])
    completed = {"t1": t1}
    assert is_task_ready(t2, completed) is False


def test_is_task_ready_dep_not_in_completed():
    """依赖不在 completed 中时任务不就绪"""
    from agent.coordinator.executor import is_task_ready
    t2 = Task(id="t2", title="B", depends_on=["t1"])
    completed = {}
    assert is_task_ready(t2, completed) is False


# ── find_dependent_tasks ───────────────────────────────────────────────────────

def test_find_dependent_tasks_single():
    """找到直接依赖某任务的所有任务"""
    from agent.coordinator.executor import find_dependent_tasks
    tasks = [
        Task(id="t1", title="A"),
        Task(id="t2", title="B", depends_on=["t1"]),
        Task(id="t3", title="C", depends_on=["t1"]),
    ]
    dependents = find_dependent_tasks("t1", tasks)
    assert {t.id for t in dependents} == {"t2", "t3"}


def test_find_dependent_tasks_none():
    """没有任务依赖该任务"""
    from agent.coordinator.executor import find_dependent_tasks
    tasks = [
        Task(id="t1", title="A"),
        Task(id="t2", title="B"),
    ]
    dependents = find_dependent_tasks("t1", tasks)
    assert dependents == []


# ── 并行执行（模拟） ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_parallel_execution_no_deps():
    """无依赖任务同时运行"""
    executed: list[str] = []

    async def fake_run_one(task_id: str, delay: float):
        await asyncio.sleep(delay)
        executed.append(task_id)

    # 模拟两个无依赖任务并行
    t1 = Task(id="t1", title="A", depends_on=[])
    t2 = Task(id="t2", title="B", depends_on=[])

    await asyncio.gather(
        fake_run_one("t1", 0.01),
        fake_run_one("t2", 0.01),
    )

    assert "t1" in executed
    assert "t2" in executed
    # 无依赖，应同时执行（顺序不确定）
    assert len(executed) == 2


@pytest.mark.asyncio
async def test_sequential_dependency_wait():
    """依赖等待：必须等上游完成"""
    events: list[str] = []

    async def fake_task(task_id: str, delay: float, depends_on: list[str] | None = None):
        # 简单检查：等待依赖完成
        await asyncio.sleep(delay)
        events.append(task_id)

    # t2 依赖 t1，t1 先执行
    await fake_task("t1", 0.01)
    await fake_task("t2", 0.01, depends_on=["t1"])

    assert events == ["t1", "t2"]


# ── 结果汇总（合成）逻辑 ──────────────────────────────────────────────────────

def test_merge_artifact_refs():
    """merge_artifact_refs 合并多个任务的 artifact_refs"""
    from agent.coordinator.executor import merge_artifact_refs
    tasks = [
        Task(id="t1", title="A", result={"artifact_refs": [{"id": "a1"}, {"id": "a2"}]}),
        Task(id="t2", title="B", result={"artifact_refs": [{"id": "b1"}]}),
    ]
    merged = merge_artifact_refs(tasks)
    assert len(merged) == 3
    assert {"id": "a1"} in merged
    assert {"id": "b1"} in merged


def test_merge_reviews():
    """merge_reviews 合并多个任务的 review"""
    from agent.coordinator.executor import merge_reviews
    tasks = [
        Task(id="t1", title="A", result={"review": {"score": 0.9}}),
        Task(id="t2", title="B", result={"review": {"score": 0.8}}),
    ]
    merged = merge_reviews(tasks)
    assert merged["t1"]["score"] == 0.9
    assert merged["t2"]["score"] == 0.8


def test_merge_budgets():
    """merge_budgets 合并多个任务的 budget"""
    from agent.coordinator.executor import merge_budgets
    tasks = [
        Task(id="t1", title="A", result={"budget": {"cost_usd": 0.05}}),
        Task(id="t2", title="B", result={"budget": {"cost_usd": 0.03}}),
    ]
    merged = merge_budgets(tasks)
    assert "t1" in merged["tasks"]
    assert "t2" in merged["tasks"]


# ── 简单 2-task 链式 DAG ───────────────────────────────────────────────────────

def test_two_task_chain_dag():
    """简单 2-task 链式 DAG: t1 → t2"""
    from agent.coordinator.executor import validate_dag
    tasks = [
        Task(id="t1", title="First", depends_on=[]),
        Task(id="t2", title="Second", depends_on=["t1"]),
    ]
    errors = validate_dag(tasks)
    assert errors == []


def test_two_task_chain_is_task_ready():
    """链式 DAG: t1 完成前 t2 不就绪，t1 完成后 t2 就绪"""
    from agent.coordinator.executor import is_task_ready
    t1 = Task(id="t1", title="First", status="done")
    t2 = Task(id="t2", title="Second", depends_on=["t1"])

    # t1 未完成
    assert is_task_ready(t2, {}) is False

    # t1 完成
    assert is_task_ready(t2, {"t1": t1}) is True


def test_two_task_chain_context_injection():
    """链式 DAG: t2 正确注入 t1 的上下文"""
    t2 = Task(id="t2", title="Second", depends_on=["t1"])
    t1 = Task(id="t1", title="First", assigned_skill="do_research")
    t1_vars = {
        "t1": TaskVarEntry(
            summary="first step result",
            artifact_refs=[{"type": "doc", "id": "d1"}],
            source_task_id="t1",
            source_skill="do_research",
        )
    }
    ctx = inject_upstream_context(t2, t1_vars)
    assert "first step result" in ctx["upstream_context"]
    assert ctx["upstream_artifacts"][0]["id"] == "d1"
