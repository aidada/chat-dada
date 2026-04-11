from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from langgraph.config import get_stream_writer

from agent.coordinator.state import (
    CoordinatorConfig,
    CoordinatorState,
    DAGFailureStrategy,
    ExecutionMode,
    SkillContext,
    SkillResult,
    Task,
    TaskVarEntry,
    build_task_vars_entry,
    inject_upstream_context,
)
from agent.platform.emit import safe_emit_progress as _safe_emit

_log = logging.getLogger("chatdada.coordinator.executor")

MAX_DAG_DEPTH = 10


# ── DAG 验证 ──────────────────────────────────────────────────────────────────

def calculate_task_depth(task: Task, task_map: dict[str, Task], memo: dict[str, int] | None = None) -> int:
    if memo is None:
        memo = {}
    if task.id in memo:
        return memo[task.id]
    if not task.depends_on:
        memo[task.id] = 0
        return 0
    max_depth = max(
        calculate_task_depth(task_map[dep_id], task_map, memo)
        for dep_id in task.depends_on
        if dep_id in task_map
    )
    depth = max_depth + 1
    memo[task.id] = depth
    return depth


def validate_dag(task_dag: list[Task]) -> list[str]:
    """验证 DAG 合法性：悬空引用、循环依赖、深度超限"""
    errors: list[str] = []
    task_map = {t.id: t for t in task_dag}

    # 1. 悬空引用检查
    for task in task_dag:
        for dep_id in task.depends_on:
            if dep_id not in task_map:
                errors.append(f"Task {task.id} depends on non-existent task {dep_id}")

    if errors:
        return errors

    # 2. 循环依赖检查（DFS）
    adj: dict[str, set[str]] = {t.id: set(t.depends_on) for t in task_dag}
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in task_map}

    def dfs(node: str, path: list[str]) -> list[str]:
        color[node] = GRAY
        path.append(node)
        for dep in adj[node]:
            if color[dep] == GRAY:
                cycle_start = path.index(dep)
                return path[cycle_start:] + [dep]
            if color[dep] == WHITE:
                cycle = dfs(dep, path[:])
                if cycle:
                    return cycle
        color[node] = BLACK
        return []

    for task_id in task_map:
        if color[task_id] == WHITE:
            cycle = dfs(task_id, [])
            if cycle:
                errors.append(f"Circular dependency detected: {' -> '.join(cycle)}")

    # 3. 深度检查
    if not errors:
        memo: dict[str, int] = {}
        for task in task_dag:
            try:
                depth = calculate_task_depth(task, task_map, memo)
                if depth > MAX_DAG_DEPTH:
                    errors.append(f"Task {task.id} exceeds max depth: {depth} > {MAX_DAG_DEPTH}")
            except RecursionError:
                errors.append(f"Task {task.id} depth calculation failed (possible cycle)")

    return errors


def find_dependent_tasks(task_id: str, task_dag: list[Task]) -> list[Task]:
    """找到直接依赖某任务的所有任务"""
    return [t for t in task_dag if task_id in t.depends_on]


def is_task_ready(task: Task, completed: dict[str, Task]) -> bool:
    """检查所有依赖任务已完成"""
    for dep_id in task.depends_on:
        if dep_id not in completed:
            return False
        if completed[dep_id].status != "done":
            return False
    return True


# ── 结果合并 ──────────────────────────────────────────────────────────────────

def merge_artifact_refs(tasks: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for task in tasks:
        if task.result and isinstance(task.result, dict):
            refs.extend(task.result.get("artifact_refs", []))
    return refs


def merge_reviews(tasks: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for task in tasks:
        if task.result and isinstance(task.result, dict):
            review = task.result.get("review", {})
            if review:
                merged[task.id] = review
    return merged


def merge_budgets(tasks: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {"total_cost_usd": 0.0, "tasks": {}}
    for task in tasks:
        if task.result and isinstance(task.result, dict):
            budget = task.result.get("budget", {})
            if budget:
                merged["tasks"][task.id] = budget
    return merged


# ── LLM 辅助 ─────────────────────────────────────────────────────────────────

async def _call_llm_json(messages: list[dict], *, description: str = "") -> dict | list | None:
    """调用 LLM 并解析 JSON 输出"""
    try:
        from core.models import get_llm, response_text
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = get_llm("orchestrator")
        lc_messages = []
        for msg in messages:
            if msg["role"] == "system":
                lc_messages.append(SystemMessage(content=msg["content"]))
            else:
                lc_messages.append(HumanMessage(content=msg["content"]))

        response = await llm.ainvoke(lc_messages)
        text = response_text(response)

        # 提取 JSON（处理 markdown 代码块）
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3].strip()

        return json.loads(text)
    except Exception as exc:
        _log.exception("LLM call failed: %s", description)
        return None


# ── 节点：decompose_tasks ─────────────────────────────────────────────────────

async def decompose_tasks_node(state: CoordinatorState) -> dict[str, Any]:
    """LLM 生成任务 DAG"""
    # P1 DAG resume: task_dag already restored from interrupt state — skip decomposition
    if state.get("task_dag"):
        return {}

    from agent.coordinator.prompts import build_decompose_tasks_prompt
    from agent.coordinator.skills import skill_registry

    goal = state.get("original_goal", "")
    skill_summary = skill_registry.skill_summary_for_llm()
    trace_id = state.get("trace_id", "")

    _safe_emit("step", {"content": "分解任务为 DAG...", "node": "decompose_tasks", "trace_id": trace_id})

    messages = build_decompose_tasks_prompt(goal, skill_summary)
    result = await _call_llm_json(messages, description="decompose_tasks")

    tasks: list[Task] = []
    if result and isinstance(result, dict):
        task_dicts = result.get("tasks", [])
        for td in task_dicts:
            try:
                task = Task(
                    id=str(td.get("id", f"t{len(tasks)+1}")),
                    title=str(td.get("title", "")),
                    description=str(td.get("description", "")),
                    depends_on=list(td.get("depends_on", [])),
                    assigned_skill=str(td.get("assigned_skill", "")),
                    input_data=dict(td.get("input_data", {})),
                    status="pending",
                )
                tasks.append(task)
            except Exception as e:
                _log.warning("Failed to parse task: %s", e)

    if not tasks:
        # fallback: 单任务
        tasks = [Task(
            id="t1",
            title=goal[:50],
            description=goal,
            depends_on=[],
            assigned_skill="do_research",
            input_data={"query": goal},
            status="pending",
        )]

    # 验证 DAG
    dag_errors = validate_dag(tasks)
    if dag_errors:
        _log.warning("DAG validation errors: %s", dag_errors)
        # 简单修复：去掉有问题的依赖
        for task in tasks:
            task_ids = {t.id for t in tasks}
            task.depends_on = [d for d in task.depends_on if d in task_ids]
        # 重新验证（循环依赖无法简单修复，直接清空）
        if validate_dag(tasks):
            for task in tasks:
                task.depends_on = []

    _safe_emit("task_dag", {
        "tasks": [{"id": t.id, "title": t.title, "assigned_skill": t.assigned_skill} for t in tasks],
        "status": "generated",
        "trace_id": trace_id,
    })

    pending_tasks = [t.id for t in tasks]
    return {
        "task_dag": tasks,
        "pending_tasks": pending_tasks,
        "running_tasks": {},
        "completed_tasks": {},
        "failed_tasks": {},
        "task_vars": {},
        "skill_runs": {},
    }


# ── 节点：assign_skills ───────────────────────────────────────────────────────

async def assign_skills_node(state: CoordinatorState) -> dict[str, Any]:
    """验证/调整技能分配"""
    from agent.coordinator.skills import skill_registry

    task_dag = state.get("task_dag") or []
    for task in task_dag:
        if not skill_registry.is_registered(task.assigned_skill):
            _log.warning("Unknown skill %s for task %s, fallback to do_research", task.assigned_skill, task.id)
            task.assigned_skill = "do_research"
    return {"task_dag": task_dag}


# ── 节点：execute_tasks ───────────────────────────────────────────────────────

async def execute_tasks_node(state: CoordinatorState) -> dict[str, Any]:
    """并行执行就绪任务"""
    from agent.coordinator.skills import run_skill_via_adapter, skill_registry
    from agent.coordinator.skills import _make_skill_interrupt_bridge

    task_dag = state.get("task_dag") or []
    completed = dict(state.get("completed_tasks") or {})
    failed = dict(state.get("failed_tasks") or {})
    running = dict(state.get("running_tasks") or {})
    task_vars = dict(state.get("task_vars") or {})
    skill_runs = dict(state.get("skill_runs") or {})
    config: CoordinatorConfig = state.get("config") or CoordinatorConfig()
    trace_id = state.get("trace_id", "")
    coordinator_task_id = state.get("trace_id", str(uuid.uuid4()))

    # 找就绪任务
    ready_tasks = [
        t for t in task_dag
        if t.status == "pending" and is_task_ready(t, completed)
    ]

    # 限制并行数
    ready_tasks = ready_tasks[:config.max_parallel_tasks]

    if not ready_tasks:
        return {}

    # 追踪每个任务的 skill_invocation_id，供中断处理器读取
    task_invocation_ids: dict[str, str] = {}

    async def run_one_task(task: Task) -> tuple[str, SkillResult]:
        runner = skill_registry.get_runner(task.assigned_skill)
        if runner is None:
            return task.id, SkillResult(status="error", error=f"Skill not found: {task.assigned_skill}")

        # 注入上游上下文
        upstream = inject_upstream_context(task, task_vars)
        merged_input = {**task.input_data, **upstream}

        skill_invocation_id = f"{task.id}_{uuid.uuid4().hex[:8]}"
        # 记录供中断处理器读取
        task_invocation_ids[task.id] = skill_invocation_id

        context = SkillContext(
            coordinator_task_id=coordinator_task_id,
            skill_invocation_id=skill_invocation_id,
            skill_name=task.assigned_skill,
            trace_id=trace_id,
            request_payload={"report_profile": config.report_profile},
            clarification_history=list(state.get("clarification_history") or []),
            task_vars=task_vars,
            upstream_artifacts=upstream.get("upstream_artifacts", []),
        )
        # 设置中断桥接
        context.request_interrupt_fn = _make_skill_interrupt_bridge(coordinator_task_id, skill_invocation_id)

        task.status = "running"
        task.start_time = __import__("time").monotonic()

        _safe_emit("task_start", {
            "task_id": task.id,
            "skill": task.assigned_skill,
            "trace_id": trace_id,
        })

        try:
            result = await asyncio.wait_for(
                run_skill_via_adapter(runner, merged_input, context),
                timeout=task.timeout_seconds or config.task_timeout_seconds,
            )
        except asyncio.TimeoutError:
            result = SkillResult(
                status="timeout",
                error=f"Task exceeded timeout of {task.timeout_seconds}s"
            )
        except Exception as exc:
            exc_type = type(exc).__name__
            if "GraphInterrupt" in exc_type or "Interrupt" in exc_type:
                raise
            result = SkillResult(status="error", error=str(exc))

        task.end_time = __import__("time").monotonic()

        _safe_emit("task_complete", {
            "task_id": task.id,
            "status": result.status,
            "trace_id": trace_id,
            "execution_time": result.execution_time_seconds,
        })

        return task.id, result

    # 并行执行
    task_results: list[tuple[str, SkillResult]] = []
    try:
        task_results = await asyncio.gather(*[run_one_task(t) for t in ready_tasks])
    except Exception as exc:
        exc_type = type(exc).__name__
        if "GraphInterrupt" in exc_type or "Interrupt" in exc_type:
            # 中断时仅保留 resume 所需的会话上下文；DAG 状态由 checkpoint 负责持久化。
            # 找出触发中断的任务（最后一个已记录 invocation_id 的 running 任务）
            interrupted_task = next(
                (t for t in ready_tasks if t.id in task_invocation_ids and t.status == "running"),
                ready_tasks[0] if ready_tasks else None,
            )
            interrupted_task_id = interrupted_task.id if interrupted_task else ""
            interrupted_skill = interrupted_task.assigned_skill if interrupted_task else ""
            interrupted_invocation_id = task_invocation_ids.get(interrupted_task_id, "")
            return {
                "interrupt_state": {
                    "interrupted_by": "skill_interrupt",
                    "task_id": interrupted_task_id,
                    "skill": interrupted_skill,
                    "skill_invocation_id": interrupted_invocation_id,
                    "coordinator_task_id": coordinator_task_id,
                },
                "running_tasks": {t.id: t for t in ready_tasks},
            }
        raise

    # 处理结果
    for task_id, result in task_results:
        task = next((t for t in task_dag if t.id == task_id), None)
        if task is None:
            continue

        if result.status == "interrupted":
            return {
                "interrupt_state": result.resume_metadata or {"task_id": task_id},
                "latest_checkpoint_id": result.latest_checkpoint_id,
                "running_tasks": {task_id: task},
            }

        if result.status in ("ok",):
            task.status = "done"
            task.result = {
                "result": result.result,
                "artifact_refs": result.artifact_refs,
                "review": result.review,
                "budget": result.budget,
                "strategy": result.strategy,
            }
            completed[task_id] = task
            # 写入 task_vars
            task_vars[task_id] = build_task_vars_entry(task, result)
            skill_runs[task_id] = {
                "status": "done",
                "latest_checkpoint_id": result.latest_checkpoint_id,
                "resume_metadata": result.resume_metadata,
            }
        else:
            task.status = "failed"
            task.error = result.error or result.status
            failed[task_id] = task
            skill_runs[task_id] = {"status": task.status, "error": task.error}

    return {
        "task_dag": task_dag,
        "completed_tasks": completed,
        "failed_tasks": failed,
        "running_tasks": {},
        "task_vars": task_vars,
        "skill_runs": skill_runs,
    }


# ── 节点：handle_task_result ──────────────────────────────────────────────────

async def handle_task_result_node(state: CoordinatorState) -> dict[str, Any]:
    """处理已完成任务结果（execute_tasks 已集成，此节点作为 passthrough）"""
    return {}


# ── 节点：check_dependencies ─────────────────────────────────────────────────

async def check_dependencies_node(state: CoordinatorState) -> dict[str, Any]:
    """检查依赖状态，更新 pending_tasks"""
    task_dag = state.get("task_dag") or []
    completed = state.get("completed_tasks") or {}
    failed = state.get("failed_tasks") or {}
    config: CoordinatorConfig = state.get("config") or CoordinatorConfig()

    # 检查总失败数
    total_failures = len(failed)
    if total_failures >= config.max_total_failures:
        _safe_emit("error", {"type": "max_total_failures", "count": total_failures})
        # 取消所有 pending 任务
        for task in task_dag:
            if task.status == "pending":
                task.status = "cancelled"
                failed[task.id] = task
        return {"task_dag": task_dag, "failed_tasks": failed, "pending_tasks": []}

    # 按失败策略处理
    if config.failure_strategy == DAGFailureStrategy.STOP_ALL and failed:
        for task in task_dag:
            if task.status == "pending":
                task.status = "cancelled"
        return {"task_dag": task_dag, "pending_tasks": []}

    elif config.failure_strategy == DAGFailureStrategy.STOP_DEPENDENTS and failed:
        failed_ids = set(failed.keys())
        for task in task_dag:
            if task.status == "pending" and any(d in failed_ids for d in task.depends_on):
                task.status = "cancelled"
                failed[task.id] = task

    # 更新 pending_tasks（排除已完成/失败/running 的）
    done_or_failed = set(completed.keys()) | set(failed.keys())
    pending_tasks = [
        t.id for t in task_dag
        if t.status == "pending" and t.id not in done_or_failed
    ]

    return {
        "task_dag": task_dag,
        "failed_tasks": failed,
        "pending_tasks": pending_tasks,
    }


# ── 节点：synthesize ──────────────────────────────────────────────────────────

async def synthesize_node(state: CoordinatorState) -> dict[str, Any]:
    """汇总所有已完成任务的结果"""
    from agent.coordinator.prompts import build_synthesis_prompt

    completed = state.get("completed_tasks") or {}
    trace_id = state.get("trace_id", "")

    _safe_emit("step", {"content": "汇总执行结果...", "node": "synthesize", "trace_id": trace_id})

    if not completed:
        failed = state.get("failed_tasks") or {}
        if failed:
            failed_summaries = []
            for t in failed.values():
                err = t.error or "unknown error"
                failed_summaries.append(f"{t.title}（{t.id}）: {err}")
            error_detail = "; ".join(failed_summaries)
            _safe_emit("error", {"type": "all_tasks_failed", "failed_tasks": list(failed.keys())})
            return {
                "final_result": f"任务执行失败：{error_detail}",
                "artifact_refs": [],
                "review": {"error": "all_tasks_failed", "failed_tasks": list(failed.keys())},
                "budget": {},
                "strategy_trace": [],
            }
        return {
            "final_result": "所有任务执行完成，但未产生有效结果。",
            "artifact_refs": [],
            "review": {},
            "budget": {},
            "strategy_trace": [],
        }

    # 找最终任务（没有被其他任务依赖）
    all_deps: set[str] = set()
    for task in completed.values():
        all_deps.update(task.depends_on)

    final_tasks = [
        task for task_id, task in completed.items()
        if task_id not in all_deps
    ]

    if not final_tasks:
        final_tasks = list(completed.values())

    # 合并结构化字段
    merged_artifacts: list[dict[str, Any]] = []
    merged_review: dict[str, Any] = {}
    merged_budget: dict[str, Any] = {"tasks": {}}
    strategy_trace: list[str] = []

    for task in completed.values():
        if task.result and isinstance(task.result, dict):
            merged_artifacts.extend(task.result.get("artifact_refs", []))
            review = task.result.get("review", {})
            if review:
                merged_review[task.id] = review
            budget = task.result.get("budget", {})
            if budget:
                merged_budget["tasks"][task.id] = budget
            strategy = task.result.get("strategy", "")
            if strategy:
                strategy_trace.append(strategy)
        if task.assigned_skill:
            strategy_trace.append(task.assigned_skill)

    # 生成 final_result
    if len(final_tasks) == 1:
        task = final_tasks[0]
        final_text = ""
        if task.result and isinstance(task.result, dict):
            final_text = str(task.result.get("result", ""))
        if not final_text:
            final_text = f"任务 '{task.title}' 已完成。"
    else:
        # 多个最终任务，用 LLM 汇总
        prompt = build_synthesis_prompt(completed, final_tasks)
        try:
            from core.models import get_llm, response_text
            from langchain_core.messages import HumanMessage
            llm = get_llm("orchestrator")
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            final_text = response_text(response)
        except Exception:
            final_text = "\n\n".join(
                f"## {t.title}\n{t.result.get('result', '') if isinstance(t.result, dict) else ''}"
                for t in final_tasks
            )

    return {
        "final_result": final_text,
        "artifact_refs": merged_artifacts,
        "review": merged_review,
        "budget": merged_budget,
        "strategy_trace": list(dict.fromkeys(strategy_trace)),  # 去重保序
    }


# ── 节点：handle_failure ──────────────────────────────────────────────────────

async def handle_failure_node(state: CoordinatorState) -> dict[str, Any]:
    """处理失败任务（由 check_dependencies 委托）"""
    failed = state.get("failed_tasks") or {}
    _safe_emit("error", {"type": "dag_failure", "failed_tasks": list(failed.keys())})
    return {
        "final_result": f"任务执行失败：{', '.join(failed.keys())}",
        "artifact_refs": [],
        "review": {},
        "budget": {},
        "strategy_trace": [],
    }


# ── 路由函数 ──────────────────────────────────────────────────────────────────

def route_after_check_dependencies(state: CoordinatorState) -> str:
    """检查是否还有待执行任务"""
    pending = state.get("pending_tasks") or []
    failed = state.get("failed_tasks") or {}
    completed = state.get("completed_tasks") or {}
    task_dag = state.get("task_dag") or []

    # 若所有任务都完成或取消
    active_tasks = [t for t in task_dag if t.status in ("pending", "running")]
    if not active_tasks:
        return "synthesize"

    # 若有挂起任务
    if pending:
        return "execute_tasks"

    # 若有失败但没有 pending（失败策略已处理）
    return "synthesize"


__all__ = [
    "validate_dag",
    "calculate_task_depth",
    "find_dependent_tasks",
    "is_task_ready",
    "decompose_tasks_node",
    "assign_skills_node",
    "execute_tasks_node",
    "handle_task_result_node",
    "check_dependencies_node",
    "synthesize_node",
    "handle_failure_node",
    "route_after_check_dependencies",
    "merge_artifact_refs",
    "merge_reviews",
    "merge_budgets",
]
