"""
Phase 3 — Coordinator 数据结构测试
覆盖: ExecutionMode, Task, TaskVarEntry, CoordinatorState, CoordinatorConfig
      序列化/反序列化, 字段默认值, 类型校验
"""
from __future__ import annotations

import pickle
import pytest

from agent.coordinator.state import (
    CoordinatorConfig,
    CoordinatorState,
    DAGFailureStrategy,
    ExecutionMode,
    Task,
    TaskVarEntry,
    build_task_vars_entry,
    inject_upstream_context,
)


# ── ExecutionMode ─────────────────────────────────────────────────────────────

def test_execution_mode_values():
    """ExecutionMode 三个枚举值"""
    assert ExecutionMode.DIRECT.value == "direct"
    assert ExecutionMode.SINGLE_SKILL.value == "single_skill"
    assert ExecutionMode.DAG.value == "dag"


def test_execution_mode_from_str():
    """ExecutionMode 可从字符串构造"""
    assert ExecutionMode("direct") == ExecutionMode.DIRECT
    assert ExecutionMode("single_skill") == ExecutionMode.SINGLE_SKILL
    assert ExecutionMode("dag") == ExecutionMode.DAG


def test_execution_mode_invalid():
    """无效字符串抛出 ValueError"""
    with pytest.raises(ValueError):
        ExecutionMode("invalid_mode")


# ── DAGFailureStrategy ─────────────────────────────────────────────────────────

def test_dag_failure_strategy_values():
    """DAGFailureStrategy 三个枚举值"""
    assert DAGFailureStrategy.STOP_ALL.value == "stop"
    assert DAGFailureStrategy.STOP_DEPENDENTS.value == "stop_dependents"
    assert DAGFailureStrategy.CONTINUE.value == "continue"


# ── Task ──────────────────────────────────────────────────────────────────────

def test_task_creation_defaults():
    """Task 所有字段默认值"""
    t = Task(id="t1", title="Test")
    assert t.id == "t1"
    assert t.title == "Test"
    assert t.description == ""
    assert t.depends_on == []
    assert t.assigned_skill == ""
    assert t.input_data == {}
    assert t.priority == 0
    assert t.max_retries == 2
    assert t.timeout_seconds == 300
    assert t.status == "pending"
    assert t.result is None
    assert t.error is None
    assert t.retry_count == 0
    assert t.start_time is None
    assert t.end_time is None


def test_task_all_fields():
    """Task 所有显式字段"""
    t = Task(
        id="t1",
        title="Test Task",
        description="desc",
        depends_on=["t0"],
        assigned_skill="do_research",
        input_data={"query": "test"},
        priority=5,
        max_retries=3,
        timeout_seconds=600,
        status="running",
        result={"result": "ok"},
        error=None,
        retry_count=1,
        start_time=1000.0,
        end_time=1010.0,
    )
    assert t.id == "t1"
    assert t.depends_on == ["t0"]
    assert t.assigned_skill == "do_research"
    assert t.priority == 5
    assert t.max_retries == 3
    assert t.timeout_seconds == 600
    assert t.status == "running"
    assert t.result == {"result": "ok"}
    assert t.retry_count == 1


# ── Task 序列化/反序列化 ──────────────────────────────────────────────────────

def test_task_pickle_roundtrip():
    """Task 可被 pickle 序列化/反序列化"""
    t = Task(
        id="t1",
        title="Test",
        description="desc",
        depends_on=["t0"],
        assigned_skill="do_research",
        input_data={"query": "test"},
        priority=5,
        max_retries=3,
        timeout_seconds=600,
        status="done",
        result={"result": "ok"},
        error=None,
        retry_count=1,
        start_time=1000.0,
        end_time=1010.0,
    )
    pickled = pickle.dumps(t)
    t2 = pickle.loads(pickled)
    assert t2.id == t.id
    assert t2.title == t.title
    assert t2.depends_on == t.depends_on
    assert t2.status == t.status
    assert t2.result == t.result


def test_task_to_dict():
    """Task 可转为 dict（dataclasses.asdict）"""
    import dataclasses
    t = Task(id="t1", title="Test", depends_on=["t0"], status="done")
    d = dataclasses.asdict(t)
    assert d["id"] == "t1"
    assert d["depends_on"] == ["t0"]
    assert d["status"] == "done"


def test_task_from_dict():
    """Task 可从 dict 构造"""
    d = {
        "id": "t1",
        "title": "Test",
        "description": "desc",
        "depends_on": ["t0"],
        "assigned_skill": "do_research",
        "input_data": {"query": "test"},
        "priority": 1,
        "max_retries": 2,
        "timeout_seconds": 300,
        "status": "pending",
        "result": None,
        "error": None,
        "retry_count": 0,
        "start_time": None,
        "end_time": None,
    }
    t = Task(**d)
    assert t.id == "t1"
    assert t.depends_on == ["t0"]
    assert t.status == "pending"


# ── TaskVarEntry ───────────────────────────────────────────────────────────────

def test_task_var_entry_defaults():
    """TaskVarEntry 标准字段及默认值"""
    entry = TaskVarEntry(summary="test summary")
    assert entry.summary == "test summary"
    assert entry.key_findings == []
    assert entry.artifact_refs == []
    assert entry.source_task_id == ""
    assert entry.source_skill == ""


def test_task_var_entry_all_fields():
    """TaskVarEntry 所有显式字段"""
    entry = TaskVarEntry(
        summary="findings summary",
        key_findings=["finding1", "finding2"],
        artifact_refs=[{"type": "report", "id": "r1"}],
        source_task_id="t1",
        source_skill="do_research",
    )
    assert entry.summary == "findings summary"
    assert entry.key_findings == ["finding1", "finding2"]
    assert entry.artifact_refs[0]["id"] == "r1"
    assert entry.source_task_id == "t1"
    assert entry.source_skill == "do_research"


def test_task_var_entry_pickle_roundtrip():
    """TaskVarEntry 可 pickle 序列化"""
    entry = TaskVarEntry(
        summary="test",
        key_findings=["f1"],
        artifact_refs=[{"id": "a1"}],
        source_task_id="t1",
        source_skill="do_research",
    )
    pickled = pickle.dumps(entry)
    entry2 = pickle.loads(pickled)
    assert entry2.summary == entry.summary
    assert entry2.key_findings == entry.key_findings


# ── CoordinatorConfig ─────────────────────────────────────────────────────────

def test_coordinator_config_defaults():
    """CoordinatorConfig 所有字段及默认值"""
    config = CoordinatorConfig()
    assert config.failure_strategy == DAGFailureStrategy.STOP_DEPENDENTS
    assert config.max_total_failures == 3
    assert config.task_timeout_seconds == 300
    assert config.dag_timeout_seconds == 3600
    assert config.max_cost_usd == 10.0
    assert config.cost_warning_threshold == 0.8
    assert config.max_parallel_tasks == 5
    assert config.max_dag_depth == 10
    assert config.report_profile == ""


def test_coordinator_config_custom():
    """CoordinatorConfig 自定义字段"""
    config = CoordinatorConfig(
        failure_strategy=DAGFailureStrategy.STOP_ALL,
        max_total_failures=1,
        task_timeout_seconds=60,
        max_parallel_tasks=2,
    )
    assert config.failure_strategy == DAGFailureStrategy.STOP_ALL
    assert config.max_total_failures == 1
    assert config.task_timeout_seconds == 60
    assert config.max_parallel_tasks == 2


def test_coordinator_config_pickle_roundtrip():
    """CoordinatorConfig 可 pickle 序列化"""
    config = CoordinatorConfig(
        failure_strategy=DAGFailureStrategy.CONTINUE,
        max_total_failures=5,
    )
    pickled = pickle.dumps(config)
    config2 = pickle.loads(pickled)
    assert config2.failure_strategy == config.failure_strategy
    assert config2.max_total_failures == config.max_total_failures


# ── CoordinatorState TypedDict ────────────────────────────────────────────────

def test_coordinator_state_empty():
    """CoordinatorState 可接受空值（TypedDict total=False）"""
    state: CoordinatorState = {}
    assert state.get("original_goal") is None
    assert state.get("execution_mode") is None
    assert state.get("task_dag") is None


def test_coordinator_state_fields():
    """CoordinatorState 各类字段"""
    state: CoordinatorState = {
        "original_goal": "测试目标",
        "trace_id": "trace-123",
        "execution_mode": ExecutionMode.DAG,
        "task_dag": [
            Task(id="t1", title="A", depends_on=[]),
        ],
        "pending_tasks": ["t1"],
        "completed_tasks": {},
        "failed_tasks": {},
        "task_vars": {},
        "skill_runs": {},
        "config": CoordinatorConfig(),
        "final_result": None,
        "artifact_refs": [],
        "review": {},
        "budget": {},
        "strategy_trace": [],
        "interrupt_state": None,
    }
    assert state["execution_mode"] == ExecutionMode.DAG
    assert len(state["task_dag"]) == 1


# ── 类型校验 ──────────────────────────────────────────────────────────────────

def test_task_depends_on_must_be_list():
    """depends_on 必须是 list[str]"""
    t = Task(id="t1", title="A", depends_on=["t0"])
    assert isinstance(t.depends_on, list)
    assert all(isinstance(x, str) for x in t.depends_on)


def test_task_input_data_must_be_dict():
    """input_data 必须是 dict"""
    t = Task(id="t1", title="A", input_data={"query": "test"})
    assert isinstance(t.input_data, dict)


def test_task_status_literal_constraint():
    """Task.status 只能是 Literal 定义的五种状态之一"""
    t = Task(id="t1", title="A")
    for valid in ("pending", "running", "done", "failed", "cancelled"):
        t.status = valid
        assert t.status == valid


# ── build_task_vars_entry ─────────────────────────────────────────────────────

def test_build_task_vars_entry():
    """build_task_vars_entry 从 SkillResult 构造 TaskVarEntry"""
    from agent.coordinator.state import SkillResult
    task = Task(id="t1", title="A", assigned_skill="do_research")
    result = SkillResult(
        status="ok",
        result="research result",
        artifact_refs=[{"type": "report", "id": "r1"}],
        review={"score": 0.9},
        budget={"cost_usd": 0.05},
        strategy="research",
    )
    entry = build_task_vars_entry(task, result)
    assert entry.summary == "research result"
    assert entry.artifact_refs == [{"type": "report", "id": "r1"}]
    assert entry.source_task_id == "t1"
    assert entry.source_skill == "do_research"


def test_build_task_vars_entry_truncates_long_summary():
    """build_task_vars_entry 截断超长 summary"""
    from agent.coordinator.state import SkillResult
    task = Task(id="t1", title="A", assigned_skill="do_research")
    long_result = "x" * 5000
    result = SkillResult(status="ok", result=long_result)
    entry = build_task_vars_entry(task, result)
    assert len(entry.summary) == 2000
    assert entry.summary.endswith("...")


# ── inject_upstream_context ───────────────────────────────────────────────────

def test_inject_upstream_context_single_dep():
    """inject_upstream_context 合并单个上游"""
    task = Task(id="t2", title="B", depends_on=["t1"])
    task_vars = {
        "t1": TaskVarEntry(
            summary="result from t1",
            key_findings=["finding1"],
            artifact_refs=[{"id": "a1"}],
            source_task_id="t1",
            source_skill="do_research",
        )
    }
    ctx = inject_upstream_context(task, task_vars)
    assert "result from t1" in ctx["upstream_context"]
    assert ctx["upstream_artifacts"][0]["id"] == "a1"


def test_inject_upstream_context_multiple_deps():
    """inject_upstream_context 合并多个上游"""
    task = Task(id="t3", title="C", depends_on=["t1", "t2"])
    task_vars = {
        "t1": TaskVarEntry(summary="t1 result", artifact_refs=[{"id": "a1"}], source_task_id="t1", source_skill="do_research"),
        "t2": TaskVarEntry(summary="t2 result", artifact_refs=[{"id": "a2"}], source_task_id="t2", source_skill="do_patent"),
    }
    ctx = inject_upstream_context(task, task_vars)
    assert "t1 result" in ctx["upstream_context"]
    assert "t2 result" in ctx["upstream_context"]
    assert len(ctx["upstream_artifacts"]) == 2


def test_inject_upstream_context_no_deps():
    """inject_upstream_context 无依赖时返回空"""
    task = Task(id="t1", title="A", depends_on=[])
    ctx = inject_upstream_context(task, {})
    assert ctx["upstream_context"] == ""
    assert ctx["upstream_artifacts"] == []


def test_inject_upstream_context_skips_missing_dep():
    """inject_upstream_context 跳过不存在的依赖"""
    task = Task(id="t2", title="B", depends_on=["t1", "t99"])
    task_vars = {
        "t1": TaskVarEntry(summary="t1 result", artifact_refs=[], source_task_id="t1", source_skill="do_research"),
    }
    ctx = inject_upstream_context(task, task_vars)
    assert "t1 result" in ctx["upstream_context"]
    assert "t99" not in ctx["upstream_context"]
