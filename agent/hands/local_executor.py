"""LocalToolExecutor — 服务端本地工具执行器。

将 agent/tools/ 下的工具函数统一封装为 Hand Contract 实现。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Awaitable

from agent.hands.protocol import ToolCall, ToolContext, ToolResult

log = logging.getLogger("chatdada.hands.local")


class LocalToolExecutor:
    """服务端工具统一执行器。

    通过 register() 注册工具函数，execute() 统一调用。
    """

    def __init__(self) -> None:
        self._tools: dict[str, Callable[..., Awaitable[Any]]] = {}

    def register(self, name: str, fn: Callable[..., Awaitable[Any]]) -> None:
        self._tools[name] = fn

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    async def prepare(self, call: ToolCall, ctx: ToolContext) -> None:
        """LocalExecutor 无需 provision，直接返回。"""
        return None

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        fn = self._tools.get(call.tool_name)
        if fn is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown tool: {call.tool_name}",
            )

        start = time.monotonic()
        try:
            result = await fn(**call.params)
            elapsed_ms = int((time.monotonic() - start) * 1000)

            output = ""
            artifacts: list[dict[str, Any]] = []

            if isinstance(result, dict):
                output = str(result.get("result", result.get("output", str(result))))
                artifacts = result.get("artifacts", [])
            elif isinstance(result, str):
                output = result
            else:
                output = str(result)

            return ToolResult(
                success=True,
                output=output,
                artifacts=artifacts,
                execution_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            log.exception("Tool %s failed", call.tool_name)
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                execution_time_ms=elapsed_ms,
            )
