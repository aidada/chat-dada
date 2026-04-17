"""ToolGateway — tool-call 事件的唯一编排入口。

职责：
- 路由 tool call 到正确的 executor (local / remote desktop)
- 作为 tool-call 业务事件的唯一权威写入点
- 为 deepagents 域提供兼容 tool adapter

设计约束：
- Remote executor 和 /tool_result 回调只负责 transport，不写业务事件
- ToolGateway 写 started/finished/failed 事件，避免重复
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

from agent.hands.protocol import ToolCall, ToolContext, ToolExecutor, ToolResult

if TYPE_CHECKING:
    from agent.session.runtime import SessionRuntime
    from agent.hands.desktop_manager import DesktopHandsManager

log = logging.getLogger("chatdada.hands.gateway")


class ToolGateway:
    """统一路由与唯一 tool-call 事件权威点。"""

    def __init__(
        self,
        local: ToolExecutor,
        session: "SessionRuntime",
        *,
        remote: ToolExecutor | None = None,
        desktop_manager: "DesktopHandsManager | None" = None,
        desktop_executor: ToolExecutor | None = None,
    ) -> None:
        self._local = local
        self._remote = remote
        self._session = session
        self._routing: dict[str, str] = {}  # tool_name → "local" | "remote"
        self._desktop_manager = desktop_manager
        self._desktop_executor = desktop_executor

    def set_route(self, tool_name: str, target: str) -> None:
        """Configure routing for a specific tool (local / remote / desktop)."""
        self._routing[tool_name] = target

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        """Execute a tool call through the gateway.

        1. Route to correct executor
        2. Emit tool.started 事件（canonical，入 DB）
        3. Execute
        4. Emit tool.completed 或 tool.failed 事件（canonical，入 DB）
        5. Return result
        """
        import uuid
        from agent.session.protocol import EventType

        # Desktop routing: if user has an active desktop connection with this tool
        target = self._routing.get(call.tool_name, "local")
        executor = self._local  # default
        executor_available = True
        availability_error = ""
        conn = None

        if self._desktop_manager is not None and self._desktop_executor is not None:
            conn = self._desktop_manager.get_connection(ctx.user_id)
            if conn is not None and conn.has_tool(call.tool_name):
                executor = self._desktop_executor
                target = "desktop"

        if self._routing.get(call.tool_name) == "desktop":
            target = "desktop"
            if (
                self._desktop_manager is None
                or self._desktop_executor is None
                or conn is None
                or not conn.has_tool(call.tool_name)
            ):
                executor_available = False
                availability_error = f"Desktop tool unavailable: {call.tool_name}"
            else:
                executor = self._desktop_executor

        if target != "desktop" and self._routing.get(call.tool_name) == "remote" and self._remote is not None:
            executor = self._remote
            target = "remote"

        # 每次工具调用生成唯一 ID，前端通过 toolCallId 关联 started/completed/failed
        tool_call_id = str(uuid.uuid4())
        stage = str(call.params.get("_cost_stage", "") or "unknown")
        public_args = {
            str(key): value
            for key, value in (call.params or {}).items()
            if not str(key).startswith("_")
        }

        await self._session.emit_event(
            call.task_id,
            EventType.TOOL_STARTED,
            {
                "toolCallId": tool_call_id,
                "name":       call.tool_name,
                "args":       public_args,
                "target":     target,
                "stage":      stage,
            },
        )

        if not executor_available:
            await self._session.emit_event(
                call.task_id,
                EventType.TOOL_FAILED,
                {
                    "toolCallId": tool_call_id,
                    "name":       call.tool_name,
                    "error":      availability_error,
                    "stage":      stage,
                },
            )
            return ToolResult(
                success=False,
                output="",
                error=availability_error,
            )

        await executor.prepare(call, ctx)
        result = await executor.execute(call, ctx)

        if result.success:
            await self._session.emit_event(
                call.task_id,
                EventType.TOOL_COMPLETED,
                {
                    "toolCallId":        tool_call_id,
                    "name":              call.tool_name,
                    "output":            result.output,
                    "execution_time_ms": result.execution_time_ms,
                    "stage":             stage,
                    **_tool_result_event_metadata(result),
                },
            )
        else:
            await self._session.emit_event(
                call.task_id,
                EventType.TOOL_FAILED,
                {
                    "toolCallId": tool_call_id,
                    "name":       call.tool_name,
                    "error":      result.error or "tool execution failed",
                    "stage":      stage,
                    **_tool_result_event_metadata(result),
                },
            )

        return result

    def bind_deepagents_tools(
        self, domain: str, task_id: str, ctx: ToolContext
    ) -> list[Any]:
        """生成 deepagents-compatible tool objects。

        内部仍走 gateway.execute() 路径，确保事件记录一致。
        过渡期实现：包装现有工具函数为 gateway-aware adapters。
        """
        if domain == "patent":
            from agent.domains.patent.tools import get_patent_tools
            domain_tools = list(get_patent_tools())
        elif domain == "zero_report":
            from agent.domains.zero_report.tools import get_zero_report_tools
            domain_tools = list(get_zero_report_tools())
        elif domain == "ppt":
            from agent.domains.ppt.tools import get_ppt_tools
            domain_tools = list(get_ppt_tools())
        elif domain == "office":
            from agent.domains.office.tools import get_office_tools
            domain_tools = list(get_office_tools())
        else:
            domain_tools = []

        if self._desktop_manager is None or ctx.user_id == "":
            return domain_tools

        from agent.hands.langchain_tools import build_desktop_langchain_tools

        descriptors = [
            descriptor
            for descriptor in self._desktop_manager.list_tool_descriptors(ctx.user_id)
            if str(descriptor.get("name", "") or "").strip()
            not in {"list_dir", "file_read", "file_write", "file_edit", "file_search", "grep", "shell", "officecli"}
        ]
        desktop_tools = build_desktop_langchain_tools(descriptors, self, ctx)
        return [*domain_tools, *desktop_tools]


def _tool_result_event_metadata(result: ToolResult) -> dict[str, Any]:
    output = str(result.output or "")
    if not output:
        return {}
    try:
        parsed = json.loads(output)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    payload: dict[str, Any] = {}
    for key in ("kind", "command", "message"):
        if parsed.get(key) is not None:
            payload[key] = parsed.get(key)
    return payload
