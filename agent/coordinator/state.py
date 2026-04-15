from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from typing_extensions import TypedDict


class ExecutionMode(str, Enum):
    DIRECT = "direct"
    SINGLE_SKILL = "single_skill"
    DAG = "dag"


class DAGFailureStrategy(str, Enum):
    STOP_ALL = "stop"
    STOP_DEPENDENTS = "stop_dependents"
    CONTINUE = "continue"


@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    assigned_skill: str = ""
    input_data: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    max_retries: int = 2
    timeout_seconds: int = 300
    status: Literal["pending", "running", "done", "failed", "cancelled"] = "pending"
    result: Any = None
    error: str | None = None
    retry_count: int = 0
    start_time: float | None = None
    end_time: float | None = None


@dataclass
class TaskVarEntry:
    summary: str
    key_findings: list[str] = field(default_factory=list)
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    source_task_id: str = ""
    source_skill: str = ""


@dataclass
class SkillContext:
    coordinator_task_id: str
    skill_invocation_id: str
    skill_name: str
    trace_id: str
    request_payload: dict[str, Any] = field(default_factory=dict)
    clarification_history: list[dict[str, Any]] = field(default_factory=list)
    latest_checkpoint_id: str | None = None
    parent_task_id: str | None = None
    task_vars: dict[str, "TaskVarEntry"] = field(default_factory=dict)
    upstream_artifacts: list[dict[str, Any]] = field(default_factory=list)
    emit_stream_event: Callable[[dict[str, Any]], None] | None = None
    request_interrupt_fn: Callable[[dict[str, Any]], Any] | None = None
    resume_metadata: dict[str, Any] = field(default_factory=dict)
    abort_signal: Any = None
    nested_depth: int = 0


@dataclass
class SkillResult:
    status: Literal["ok", "error", "interrupted", "timeout"]
    result: Any = None
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    strategy: str = ""
    latest_checkpoint_id: str | None = None
    checkpoint_data: dict[str, Any] | None = None
    resume_metadata: dict[str, Any] = field(default_factory=dict)
    raw_domain_state: dict[str, Any] | None = None
    error: str | None = None
    execution_time_seconds: float = 0.0
    cost_usd: float = 0.0


@dataclass
class CoordinatorConfig:
    failure_strategy: DAGFailureStrategy = DAGFailureStrategy.STOP_DEPENDENTS
    max_total_failures: int = 3
    task_timeout_seconds: int = 300
    dag_timeout_seconds: int = 3600
    max_cost_usd: float = 10.0
    cost_warning_threshold: float = 0.8
    max_parallel_tasks: int = 5
    max_dag_depth: int = 10
    report_profile: str = ""


class CoordinatorState(TypedDict, total=False):
    # 输入
    original_goal: str
    trace_id: str
    config: CoordinatorConfig
    available_skills: list[Any]  # list[SkillDescription] - 避免循环导入
    skill_summary: str
    conversation_context: str  # 多轮对话上下文，来自 RootState
    clarification_history: list[dict[str, Any]]  # 历史澄清记录，用于技能 resume
    source_files: list[str]
    request_user_id: str
    desktop_tool_descriptors: list[dict[str, Any]]

    # 理解阶段
    goal_understanding: str | None
    execution_mode: ExecutionMode

    # single_skill 模式
    selected_skill: str | None
    skill_input: dict[str, Any] | None
    model_hints: dict[str, dict[str, Any]] | None

    # dag 模式
    task_dag: list[Task] | None
    task_vars: dict[str, TaskVarEntry]

    # 执行状态
    pending_tasks: list[str]
    running_tasks: dict[str, Task]
    completed_tasks: dict[str, Task]
    failed_tasks: dict[str, Task]
    skill_runs: dict[str, dict[str, Any]]

    # 结果
    final_result: str | None
    artifact_refs: list[dict[str, Any]]
    review: dict[str, Any]
    budget: dict[str, Any]
    strategy_trace: list[str]
    latest_checkpoint_id: str | None

    # 中断
    interrupt_state: dict[str, Any] | None
    pending_question: dict[str, Any] | None


def build_task_vars_entry(task: Task, result: SkillResult) -> TaskVarEntry:
    """从 SkillResult 构造标准的 TaskVarEntry"""
    summary = str(result.result or "")
    if len(summary) > 2000:
        summary = summary[:1997] + "..."
    return TaskVarEntry(
        summary=summary,
        key_findings=[],
        artifact_refs=result.artifact_refs,
        source_task_id=task.id,
        source_skill=task.assigned_skill,
    )


def inject_upstream_context(
    task: Task,
    task_vars: dict[str, TaskVarEntry],
) -> dict[str, Any]:
    """为即将执行的任务注入上游依赖的摘要结果"""
    upstream_summaries: list[str] = []
    upstream_artifacts: list[dict[str, Any]] = []

    for dep_id in task.depends_on:
        entry = task_vars.get(dep_id)
        if entry is None:
            continue
        upstream_summaries.append(
            f"[{entry.source_skill}#{dep_id}] {entry.summary}"
        )
        upstream_artifacts.extend(entry.artifact_refs)

    return {
        "upstream_context": "\n\n".join(upstream_summaries),
        "upstream_artifacts": upstream_artifacts,
    }


__all__ = [
    "ExecutionMode",
    "DAGFailureStrategy",
    "Task",
    "TaskVarEntry",
    "SkillContext",
    "SkillResult",
    "CoordinatorConfig",
    "CoordinatorState",
    "build_task_vars_entry",
    "inject_upstream_context",
]
