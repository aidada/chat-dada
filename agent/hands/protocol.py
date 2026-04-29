"""Hand Contract — 工具执行的稳定接口协议。

Brain 只知道需要执行什么工具、什么参数、拿回什么结果。
Brain 不关心工具在哪里执行、背后是什么执行环境。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolCall:
    """Brain 发出的工具调用请求。"""

    tool_name: str
    params: dict[str, Any]
    task_id: str
    timeout_ms: int = 30_000
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """Hands 返回的工具执行结果。"""

    success: bool
    output: str
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    execution_time_ms: int = 0


@dataclass
class ToolContext:
    """执行上下文 — 传入 Hands 的非凭证信息。"""

    user_id: str
    task_id: str
    trace_id: str = ""
    agent_id: str = ""
    checkpoint_ns: str = ""
    policy: Any = None  # ResolvedPolicy; kept Any to avoid protocol-layer import cycles

    async def get_secret(self, key: str) -> str | None:
        """仅在 Harness / LocalExecutor 侧解析 secret。

        Remote/Desktop Hands 不直接拿到真实 secret。
        当前实现从环境变量读取，后续迁移到 VaultService。
        """
        import os
        return os.environ.get(key)


@runtime_checkable
class ToolExecutor(Protocol):
    """Hand Contract: execute + optional prepare。"""

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        """执行工具调用，返回结果。"""
        ...

    async def prepare(self, call: ToolCall, ctx: ToolContext) -> None:
        """可选的预处理：懒初始化、权限检查、环境探测。"""
        ...
