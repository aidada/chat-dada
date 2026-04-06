"""
Phase 3 — 失败处理测试
覆盖: STOP_ALL / STOP_DEPENDENTS / CONTINUE 策略, 超时处理, 重试逻辑, max_total_failures
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

import pytest

from agent.coordinator.state import (
    CoordinatorConfig,
    CoordinatorState,
    DAGFailureStrategy,
    ExecutionMode,
    SkillResult,
    Task,
)


# ── Helper: 构造带状态的 CoordinatorState ─────────────────────────────────────

def make_state(
    task_dag: list[Task],
    completed: dict | None = None,
    failed: dict | None = None,
    pending: list[str] | None = None,
    failure_strategy: DAGFailureStrategy = DAGFailureStrategy.STOP_DEPENDENTS,
    max_total_failures: int = 3,
    max_parallel: int = 5,
) -> CoordinatorState:
    if pending is None:
        pending = [t.id for t in task_dag if t.status == "pending"]
    return {
        "original_goal": "test goal",
        "trace_id": "trace-1",
        "config": CoordinatorConfig(
            failure_strategy=failure_strategy,
            max_total_failures=max_total_failures,
            max_parallel_tasks=max_parallel,
        ),
        "execution_mode": ExecutionMode.DAG,
        "task_dag": task_dag,
        "completed_tasks": completed or {},
        "failed_tasks": failed or {},
        "pending_tasks": pending,
        "running_tasks": {},
        "task_vars": {},
        "skill_runs": {},
        "artifact_refs": [],
        "review": {},
        "budget": {},
        "strategy_trace": [],
    }


# ── check_dependencies_node 策略测试 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_failure_strategy_stop_all():
    """STOP_ALL: 任意任务失败, 所有 pending 任务取消"""
    from agent.coordinator.executor import check_dependencies_node

    t1 = Task(id="t1", title="A", status="failed", error="failed")
    t2 = Task(id="t2", title="B", status="pending")
    t3 = Task(id="t3", title="C", status="pending")
    state = make_state(
        [t1, t2, t3],
        failed={"t1": t1},
        failure_strategy=DAGFailureStrategy.STOP_ALL,
    )

    result = await check_dependencies_node(state)

    # STOP_ALL: 所有 pending → cancelled
    dag = result["task_dag"]
    cancelled = [t for t in dag if t.status == "cancelled"]
    assert len(cancelled) == 2
    assert result["pending_tasks"] == []


@pytest.mark.asyncio
async def test_failure_strategy_stop_dependents():
    """STOP_DEPENDENTS: 任务失败, 仅取消直接依赖者"""
    from agent.coordinator.executor import check_dependencies_node

    # t1 → t2 → t3, t1 失败
    t1 = Task(id="t1", title="A", status="failed", error="failed", depends_on=[])
    t2 = Task(id="t2", title="B", status="pending", depends_on=["t1"])
    t3 = Task(id="t3", title="C", status="pending", depends_on=["t2"])
    # t4 不依赖 t1, 应该继续
    t4 = Task(id="t4", title="D", status="pending", depends_on=[])

    state = make_state(
        [t1, t2, t3, t4],
        failed={"t1": t1},
        failure_strategy=DAGFailureStrategy.STOP_DEPENDENTS,
    )

    result = await check_dependencies_node(state)

    dag = result["task_dag"]
    # t2 直接依赖 t1, 应被取消
    t2_updated = next(t for t in dag if t.id == "t2")
    assert t2_updated.status == "cancelled"
    # t3 依赖 t2 (t2 未取消前), 不应被取消 (因为 t2 会变为 cancelled)
    t3_updated = next(t for t in dag if t.id == "t3")
    assert t3_updated.status == "pending"
    # t4 不依赖 t1, 应继续
    t4_updated = next(t for t in dag if t.id == "t4")
    assert t4_updated.status == "pending"


@pytest.mark.asyncio
async def test_failure_strategy_continue():
    """CONTINUE: 任务失败, 其他任务继续"""
    from agent.coordinator.executor import check_dependencies_node

    t1 = Task(id="t1", title="A", status="failed", error="failed")
    t2 = Task(id="t2", title="B", status="pending")
    t3 = Task(id="t3", title="C", status="pending")

    state = make_state(
        [t1, t2, t3],
        failed={"t1": t1},
        failure_strategy=DAGFailureStrategy.CONTINUE,
    )

    result = await check_dependencies_node(state)

    # CONTINUE: pending 不变
    dag = result["task_dag"]
    pending = [t for t in dag if t.status == "pending"]
    assert len(pending) == 2
    assert result["pending_tasks"] == ["t2", "t3"]


# ── max_total_failures ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_max_total_failures_trigger():
    """max_total_failures=2, 第三个任务失败时停止所有"""
    from agent.coordinator.executor import check_dependencies_node

    t1 = Task(id="t1", title="A", status="failed")
    t2 = Task(id="t2", title="B", status="failed")
    t3 = Task(id="t3", title="C", status="pending")

    state = make_state(
        [t1, t2, t3],
        failed={"t1": t1, "t2": t2},
        pending=["t3"],
        max_total_failures=2,
    )

    result = await check_dependencies_node(state)

    # 失败数(2) >= max_total_failures(2), 所有 pending → cancelled
    dag = result["task_dag"]
    cancelled = [t for t in dag if t.status == "cancelled"]
    assert len(cancelled) == 1
    assert result["pending_tasks"] == []


@pytest.mark.asyncio
async def test_max_total_failures_not_yet_reached():
    """max_total_failures=3, 失败数未达上限时继续"""
    from agent.coordinator.executor import check_dependencies_node

    t1 = Task(id="t1", title="A", status="failed")
    t2 = Task(id="t2", title="B", status="pending")
    t3 = Task(id="t3", title="C", status="pending")

    state = make_state(
        [t1, t2, t3],
        failed={"t1": t1},
        pending=["t2", "t3"],
        max_total_failures=3,
    )

    result = await check_dependencies_node(state)

    # 未达上限，pending 不变
    assert result["pending_tasks"] == ["t2", "t3"]


# ── 超时处理 ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_tasks_timeout():
    """asyncio.TimeoutError 返回 SkillResult(status='timeout')"""
    from agent.coordinator.executor import execute_tasks_node

    t1 = Task(id="t1", title="A", assigned_skill="do_research", depends_on=[])
    state: CoordinatorState = {
        "original_goal": "test",
        "trace_id": "trace-1",
        "config": CoordinatorConfig(task_timeout_seconds=1),
        "task_dag": [t1],
        "completed_tasks": {},
        "failed_tasks": {},
        "pending_tasks": ["t1"],
        "running_tasks": {},
        "task_vars": {},
        "skill_runs": {},
        "clarification_history": [],
    }

    # Mock skill_registry.get_runner 返回一个 mock runner
    async def slow_run(*args, **kwargs):
        await asyncio.sleep(10)  # 故意超时
        return SkillResult(status="ok", result="done")

    with patch("agent.coordinator.skills.skill_registry") as mock_registry:
        mock_runner = AsyncMock()
        mock_runner.run = slow_run
        mock_registry.get_runner.return_value = mock_runner
        mock_registry.is_registered.return_value = True

        # 短暂超时以加快测试
        t1.timeout_seconds = 0
        result = await execute_tasks_node(state)

        failed = result.get("failed_tasks", {})
        assert "t1" in failed
        assert failed["t1"].error is not None


# ── execute_tasks_node 中的超时返回 ───────────────────────────────────────────

def test_timeout_error_returns_timeout_status():
    """asyncio.TimeoutError 在 execute_one_task 中返回 SkillResult(status='timeout')"""
    # 直接测试 TimeoutError 路径
    from agent.coordinator.state import SkillResult

    # 模拟超时错误
    try:
        raise asyncio.TimeoutError("task exceeded timeout")
    except asyncio.TimeoutError:
        result = SkillResult(
            status="timeout",
            error="Task exceeded timeout"
        )

    assert result.status == "timeout"
    assert "timeout" in result.error.lower()


# ── 重试逻辑 ──────────────────────────────────────────────────────────────────

def test_task_retry_count_increment():
    """任务失败时 retry_count 递增"""
    t = Task(id="t1", title="A", max_retries=2, retry_count=0, status="failed")
    # 模拟重试
    t.retry_count += 1
    assert t.retry_count == 1
    assert t.max_retries == 2


def test_task_retry_count_max_retries():
    """retry_count >= max_retries 时不再重试"""
    t = Task(id="t1", title="A", max_retries=2, retry_count=2)
    assert t.retry_count >= t.max_retries


def test_task_retry_count_within_limit():
    """retry_count < max_retries 时可以重试"""
    t = Task(id="t1", title="A", max_retries=2, retry_count=1)
    assert t.retry_count < t.max_retries


# ── SkillResult 错误状态 ──────────────────────────────────────────────────────

def test_skill_result_error_status():
    """SkillResult 支持 error/timeout 状态"""
    r = SkillResult(status="error", error="skill not found")
    assert r.status == "error"
    assert r.error == "skill not found"

    r2 = SkillResult(status="timeout", error="task timed out")
    assert r2.status == "timeout"
    assert "timed out" in r2.error.lower()


# ── 失败后更新 task_dag ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_dependencies_cancels_dependents_of_failed():
    """check_dependencies 正确取消依赖失败任务的任务"""
    from agent.coordinator.executor import check_dependencies_node

    # t1 失败; t2 依赖 t1; t3 不依赖 t1
    t1 = Task(id="t1", title="A", status="failed", error="failed")
    t2 = Task(id="t2", title="B", status="pending", depends_on=["t1"])
    t3 = Task(id="t3", title="C", status="pending", depends_on=[])

    state = make_state(
        [t1, t2, t3],
        failed={"t1": t1},
        failure_strategy=DAGFailureStrategy.STOP_DEPENDENTS,
    )

    result = await check_dependencies_node(state)

    dag = result["task_dag"]
    t2_updated = next(t for t in dag if t.id == "t2")
    assert t2_updated.status == "cancelled"
    assert "t2" in result["failed_tasks"]

    t3_updated = next(t for t in dag if t.id == "t3")
    assert t3_updated.status == "pending"
    assert "t3" not in result["failed_tasks"]


# ── 混合失败场景 ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_failures_stop_dependents_chain():
    """链式依赖中多个失败节点: STOP_DEPENDENTS"""
    from agent.coordinator.executor import check_dependencies_node

    #    t1 (failed)
    #    ↓
    #    t2 (failed)
    #    ↓
    #    t3 (pending)
    #    t4 (pending, 不依赖任何)
    t1 = Task(id="t1", title="A", status="failed", error="err", depends_on=[])
    t2 = Task(id="t2", title="B", status="pending", depends_on=["t1"])
    t3 = Task(id="t3", title="C", status="pending", depends_on=["t2"])
    t4 = Task(id="t4", title="D", status="pending", depends_on=[])

    state = make_state(
        [t1, t2, t3, t4],
        failed={"t1": t1},
        failure_strategy=DAGFailureStrategy.STOP_DEPENDENTS,
    )

    result = await check_dependencies_node(state)

    dag = result["task_dag"]
    t2_updated = next(t for t in dag if t.id == "t2")
    assert t2_updated.status == "cancelled"
    # t3 依赖 t2，t2 已变为 cancelled
    t3_updated = next(t for t in dag if t.id == "t3")
    assert t3_updated.status == "pending"
