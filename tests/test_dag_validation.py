"""
Phase 3 — DAG 验证测试
覆盖: validate_dag() 循环依赖、悬空引用、深度超限、空/单节点/多根多叶 DAG
"""
from __future__ import annotations

import pytest

from agent.coordinator.state import Task
from agent.coordinator.executor import MAX_DAG_DEPTH, validate_dag


# ── 正常 DAG ──────────────────────────────────────────────────────────────────

def test_validate_dag_valid_linear():
    """无环、无悬空引用、深度正常的线性链"""
    tasks = [
        Task(id="t1", title="A", depends_on=[]),
        Task(id="t2", title="B", depends_on=["t1"]),
        Task(id="t3", title="C", depends_on=["t2"]),
    ]
    assert validate_dag(tasks) == []


def test_validate_dag_valid_parallel():
    """无环、无悬空引用的并行 DAG（多根）"""
    tasks = [
        Task(id="t1", title="A", depends_on=[]),
        Task(id="t2", title="B", depends_on=[]),
        Task(id="t3", title="C", depends_on=["t1", "t2"]),
    ]
    assert validate_dag(tasks) == []


def test_validate_dag_valid_multiple_roots_and_leaves():
    """多根多叶 DAG"""
    tasks = [
        Task(id="t1", title="A", depends_on=[]),
        Task(id="t2", title="B", depends_on=[]),
        Task(id="t3", title="C", depends_on=["t1"]),
        Task(id="t4", title="D", depends_on=["t1", "t2"]),
        Task(id="t5", title="E", depends_on=["t3", "t4"]),
    ]
    assert validate_dag(tasks) == []


# ── 循环依赖 ──────────────────────────────────────────────────────────────────

def test_validate_dag_cycle_two_nodes():
    """A→B→A 循环"""
    tasks = [
        Task(id="t1", title="A", depends_on=["t2"]),
        Task(id="t2", title="B", depends_on=["t1"]),
    ]
    errors = validate_dag(tasks)
    assert len(errors) > 0
    assert any("Circular" in e or "cycle" in e.lower() for e in errors)


def test_validate_dag_cycle_three_nodes():
    """A→B→C→A 循环"""
    tasks = [
        Task(id="t1", title="A", depends_on=["t2"]),
        Task(id="t2", title="B", depends_on=["t3"]),
        Task(id="t3", title="C", depends_on=["t1"]),
    ]
    errors = validate_dag(tasks)
    assert len(errors) > 0
    assert any("Circular" in e or "cycle" in e.lower() for e in errors)


def test_validate_dag_self_loop():
    """自环 A→A"""
    tasks = [
        Task(id="t1", title="A", depends_on=["t1"]),
    ]
    errors = validate_dag(tasks)
    assert len(errors) > 0
    assert any("Circular" in e or "cycle" in e.lower() for e in errors)


# ── 悬空引用 ──────────────────────────────────────────────────────────────────

def test_validate_dag_dangling_single():
    """引用不存在的 task_id"""
    tasks = [
        Task(id="t1", title="A", depends_on=["t99"]),
    ]
    errors = validate_dag(tasks)
    assert len(errors) > 0
    assert any("t99" in e for e in errors)


def test_validate_dag_dangling_multiple():
    """多个悬空引用"""
    tasks = [
        Task(id="t1", title="A", depends_on=["t99"]),
        Task(id="t2", title="B", depends_on=["t88", "t77"]),
    ]
    errors = validate_dag(tasks)
    assert len(errors) > 0
    assert any("t99" in e for e in errors)
    assert any("t88" in e or "t77" in e for e in errors)


# ── 深度超限 ──────────────────────────────────────────────────────────────────

def test_validate_dag_depth_exceeded():
    """超过 MAX_DAG_DEPTH 的链"""
    tasks = []
    prev_id = None
    for i in range(MAX_DAG_DEPTH + 3):
        task_id = f"t{i+1}"
        depends_on = [prev_id] if prev_id else []
        tasks.append(Task(id=task_id, title=f"Task {i+1}", depends_on=depends_on))
        prev_id = task_id

    errors = validate_dag(tasks)
    assert len(errors) > 0
    assert any("depth" in e.lower() or "exceeds" in e.lower() for e in errors)


def test_validate_dag_depth_within_limit():
    """深度刚好在限制内"""
    tasks = []
    prev_id = None
    for i in range(MAX_DAG_DEPTH):
        task_id = f"t{i+1}"
        depends_on = [prev_id] if prev_id else []
        tasks.append(Task(id=task_id, title=f"Task {i+1}", depends_on=depends_on))
        prev_id = task_id

    assert validate_dag(tasks) == []


# ── 边界条件 ──────────────────────────────────────────────────────────────────

def test_validate_dag_empty():
    """空 DAG"""
    assert validate_dag([]) == []


def test_validate_dag_single_node():
    """只有根任务的单节点 DAG"""
    tasks = [
        Task(id="t1", title="Root", depends_on=[]),
    ]
    assert validate_dag(tasks) == []


def test_validate_dag_complex_diamond():
    """菱形 DAG: t1 → {t2, t3} → t4"""
    tasks = [
        Task(id="t1", title="A", depends_on=[]),
        Task(id="t2", title="B", depends_on=["t1"]),
        Task(id="t3", title="C", depends_on=["t1"]),
        Task(id="t4", title="D", depends_on=["t2", "t3"]),
    ]
    assert validate_dag(tasks) == []
