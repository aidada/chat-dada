"""streaming 协议定义 — Layer 1 后端实现。

本模块是 Layer 0（docs/superpowers/specs/2026-04-11-protocol-design.md）的
Python 实现，是后端协议的唯一真相源。

职责：
- 定义 23 个 category.action 格式的事件类型
- 区分 canonical（入 DB）和 transient（仅 Redis）事件
- 提供旧类型名到新类型名的映射，用于 DB 迁移和过渡期兼容
"""

from __future__ import annotations

from enum import Enum


class EventType(str, Enum):
    """协议事件类型枚举 — category.action 格式。

    命名规则：category.action，例如 "lifecycle.started"。
    持久化规则：
        canonical → 入 DB + Redis PubSub，有 seq 编号，可断线重放
        transient → 仅 Redis PubSub，无 seq，断线即丢失
    """

    # ── lifecycle/ — Task 生命周期 ─────────────────────────────────────────
    LIFECYCLE_STARTED   = "lifecycle.started"   # canonical: Task 开始执行
    LIFECYCLE_COMPLETED = "lifecycle.completed"  # canonical: Task 成功完成
    LIFECYCLE_FAILED    = "lifecycle.failed"     # canonical: Task 执行失败
    LIFECYCLE_CANCELLED = "lifecycle.cancelled"  # canonical: Task 被取消

    # ── content/ — AI 输出内容流 ──────────────────────────────────────────
    CONTENT_DELTA = "content.delta"  # transient: 增量文本片段（流式打字效果）
    CONTENT_DONE  = "content.done"   # canonical: 完整文本内容（task 完成时最终文本）

    # ── thinking/ — 思维链 ────────────────────────────────────────────────
    THINKING_DELTA = "thinking.delta"  # transient: 增量思维链片段
    THINKING_DONE  = "thinking.done"   # transient: 思维链完成信号

    # ── tool/ — 工具调用 ──────────────────────────────────────────────────
    TOOL_STARTED   = "tool.started"    # canonical: 工具调用开始
    TOOL_COMPLETED = "tool.completed"  # canonical: 工具调用成功
    TOOL_FAILED    = "tool.failed"     # canonical: 工具调用失败

    # ── interaction/ — 人机交互 ───────────────────────────────────────────
    INTERACTION_QUESTION = "interaction.question"  # canonical: 向用户提问（任务中断）
    INTERACTION_ANSWER   = "interaction.answer"    # canonical: 用户回答（任务恢复）

    # ── artifact/ — 产物管理 ──────────────────────────────────────────────
    ARTIFACT_CREATED = "artifact.created"  # canonical: 文件/产物生成
    ARTIFACT_STAGED  = "artifact.staged"   # canonical: 产物暂存到预览区

    # ── progress/ — 执行进度 ──────────────────────────────────────────────
    PROGRESS_STEP       = "progress.step"        # canonical: 步骤更新
    PROGRESS_NODE       = "progress.node"        # transient: 节点实时状态
    PROGRESS_PLAN       = "progress.plan"        # canonical: 执行计划
    PROGRESS_BRIEF      = "progress.brief"       # canonical: 简要进度描述
    PROGRESS_DAG        = "progress.dag"         # transient: DAG 拓扑/进度（高频）
    PROGRESS_CHECKPOINT = "progress.checkpoint"  # canonical: 检查点

    # ── system/ — 系统级 ──────────────────────────────────────────────────
    SYSTEM_MONITORING = "system.monitoring"  # transient: 监控指标快照
    SYSTEM_HEARTBEAT  = "system.heartbeat"   # transient: 连接保活心跳


# Canonical 事件：持久化到 DB，有单调递增 seq，断线重连后可重放（通过 seq 去重）
CANONICAL_EVENT_TYPES: frozenset[str] = frozenset({
    EventType.LIFECYCLE_STARTED.value,
    EventType.LIFECYCLE_COMPLETED.value,
    EventType.LIFECYCLE_FAILED.value,
    EventType.LIFECYCLE_CANCELLED.value,
    EventType.CONTENT_DONE.value,
    EventType.TOOL_STARTED.value,
    EventType.TOOL_COMPLETED.value,
    EventType.TOOL_FAILED.value,
    EventType.INTERACTION_QUESTION.value,
    EventType.INTERACTION_ANSWER.value,
    EventType.ARTIFACT_CREATED.value,
    EventType.ARTIFACT_STAGED.value,
    EventType.PROGRESS_STEP.value,
    EventType.PROGRESS_PLAN.value,
    EventType.PROGRESS_BRIEF.value,
    EventType.PROGRESS_CHECKPOINT.value,
})

# Transient 事件：仅 Redis PubSub，断线即丢失，不可重放，无 seq
TRANSIENT_EVENT_TYPES: frozenset[str] = frozenset({
    EventType.CONTENT_DELTA.value,
    EventType.THINKING_DELTA.value,
    EventType.THINKING_DONE.value,
    EventType.PROGRESS_NODE.value,
    EventType.PROGRESS_DAG.value,
    EventType.SYSTEM_MONITORING.value,
    EventType.SYSTEM_HEARTBEAT.value,
})


def is_transient(event_type: str) -> bool:
    """判断事件是否为 transient（仅流式推送，不入 DB）。"""
    return str(event_type) in TRANSIENT_EVENT_TYPES


# 旧类型名 → 新类型名映射，用于：
#   1. DB 迁移（session_store.py setup() 中的 SQL UPDATE）
#   2. 过渡期兼容：业务代码传入旧名时自动转换
OLD_TO_NEW_TYPE_MAP: dict[str, str] = {
    # lifecycle/ 映射
    "start":             EventType.LIFECYCLE_STARTED.value,
    "task_created":      EventType.LIFECYCLE_STARTED.value,   # 折叠
    "result":            EventType.LIFECYCLE_COMPLETED.value,
    "error":             EventType.LIFECYCLE_FAILED.value,
    "cancel_requested":  EventType.LIFECYCLE_CANCELLED.value,
    # content/ 映射
    "token":             EventType.CONTENT_DELTA.value,
    "streaming_content": EventType.CONTENT_DELTA.value,
    "result_delta":      EventType.CONTENT_DELTA.value,
    "thinking":          EventType.THINKING_DELTA.value,
    # tool/ 映射
    "tool_call_started":  EventType.TOOL_STARTED.value,
    "tool_call_finished": EventType.TOOL_COMPLETED.value,
    "tool_call_failed":   EventType.TOOL_FAILED.value,
    "skill_started":      EventType.TOOL_STARTED.value,       # skill 折叠到 tool
    "skill_finished":     EventType.TOOL_COMPLETED.value,
    "skill_failed":       EventType.TOOL_FAILED.value,
    # interaction/ 映射
    "question":   EventType.INTERACTION_QUESTION.value,
    "user_reply": EventType.INTERACTION_ANSWER.value,
    # artifact/ 映射
    "file":            EventType.ARTIFACT_CREATED.value,
    "stage_artifacts": EventType.ARTIFACT_STAGED.value,
    # progress/ 映射
    "step":           EventType.PROGRESS_STEP.value,
    "task":           EventType.PROGRESS_STEP.value,          # 折叠为 step
    "task_start":     EventType.PROGRESS_STEP.value,
    "task_complete":  EventType.PROGRESS_STEP.value,
    "task_dag":       EventType.PROGRESS_DAG.value,
    "dag_progress":   EventType.PROGRESS_DAG.value,
    "node":           EventType.PROGRESS_NODE.value,
    "plan":           EventType.PROGRESS_PLAN.value,
    "brief":          EventType.PROGRESS_BRIEF.value,
    "strategy":       EventType.PROGRESS_BRIEF.value,
    "review":         EventType.PROGRESS_BRIEF.value,
    "checkpoint":     EventType.PROGRESS_CHECKPOINT.value,
    "checkpoint_saved": EventType.PROGRESS_CHECKPOINT.value,  # 折叠
    # system/ 映射
    "monitoring":      EventType.SYSTEM_MONITORING.value,
    "monitoring_live": EventType.SYSTEM_MONITORING.value,
}
