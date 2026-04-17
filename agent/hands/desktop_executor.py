# agent/hands/desktop_executor.py
"""DesktopToolExecutor — 通过 WebSocket 在桌面端执行工具。

实现 ToolExecutor 协议。将 ToolCall 序列化为 WS 消息发送给桌面客户端，
等待 tool_result 消息返回后反序列化为 ToolResult。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from agent.hands.protocol import ToolCall, ToolContext, ToolResult
from agent.hands.desktop_manager import DesktopHandsManager

log = logging.getLogger("chatdada.hands.desktop_executor")


class DesktopToolExecutor:
    """Routes tool calls to a desktop client via WebSocket."""

    def __init__(
        self,
        manager: DesktopHandsManager,
        *,
        default_timeout_s: float = 60.0,
    ) -> None:
        self._manager = manager
        self._default_timeout_s = default_timeout_s
        # invocation_id → Future that resolves with result payload
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # task_id → monotonic expiry for fast-fail after desktop timeout
        self._timeout_cooldown_until: dict[str, float] = {}

    async def prepare(self, call: ToolCall, ctx: ToolContext) -> None:
        """No-op — permission checks happen on the client side."""
        return None

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        now = time.monotonic()
        cooldown_until = self._timeout_cooldown_until.get(call.task_id, 0.0)
        if cooldown_until > now:
            remaining = max(cooldown_until - now, 0.0)
            return ToolResult(
                success=False,
                output="",
                error=f"Desktop tool execution is cooling down after a previous timeout ({remaining:.1f}s remaining)",
            )

        conn = self._manager.get_connection(ctx.user_id)
        if conn is None:
            return ToolResult(
                success=False,
                output="",
                error="No desktop connection for this user",
            )

        invocation_id = str(uuid.uuid4())
        timeout_s = call.timeout_ms / 1000.0 if call.timeout_ms else self._default_timeout_s

        # Create future for this invocation
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[invocation_id] = future

        # Extract operation from params (structured call convention)
        operation = call.params.get("operation", call.tool_name)
        params = {k: v for k, v in call.params.items() if k != "operation"}

        start = time.monotonic()
        try:
            await conn.ws.send_json({
                "type": "tool_call",
                "id": f"msg_{invocation_id[:8]}",
                "timestamp": _iso_now(),
                "payload": {
                    "invocation_id": invocation_id,
                    "task_id": call.task_id,
                    "tool": call.tool_name,
                    "operation": operation,
                    "params": params,
                    "timeout_ms": call.timeout_ms,
                },
            })

            result_payload = await asyncio.wait_for(future, timeout=timeout_s)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self._timeout_cooldown_until.pop(call.task_id, None)

            return ToolResult(
                success=result_payload.get("success", False),
                output=result_payload.get("output", ""),
                artifacts=result_payload.get("artifacts", []),
                error=result_payload.get("error"),
                execution_time_ms=result_payload.get("execution_time_ms", elapsed_ms),
            )

        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self._timeout_cooldown_until[call.task_id] = time.monotonic() + 60.0
            log.warning(
                "Desktop tool call timed out: tool=%s inv=%s timeout=%ss",
                call.tool_name, invocation_id, timeout_s,
            )
            await self._send_cancel(conn, invocation_id, f"timeout after {timeout_s}s")
            return ToolResult(
                success=False,
                output="",
                error=f"Desktop tool call timeout after {timeout_s}s",
                execution_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            log.exception("Desktop tool call failed: %s", exc)
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                execution_time_ms=elapsed_ms,
            )
        finally:
            self._pending.pop(invocation_id, None)

    def resolve_invocation(self, invocation_id: str, payload: dict[str, Any]) -> None:
        """Called when a tool_result message arrives from the client."""
        future = self._pending.get(invocation_id)
        if future and not future.done():
            future.set_result(payload)
        else:
            log.warning("No pending invocation for %s", invocation_id)

    def cancel_invocation(self, invocation_id: str, reason: str = "cancelled") -> None:
        """Cancel a pending invocation."""
        future = self._pending.get(invocation_id)
        if future and not future.done():
            future.set_exception(asyncio.CancelledError(reason))

    async def _send_cancel(self, conn: Any, invocation_id: str, reason: str) -> None:
        try:
            await conn.ws.send_json({
                "type": "tool_cancel",
                "id": f"cancel_{invocation_id[:8]}",
                "timestamp": _iso_now(),
                "payload": {
                    "invocation_id": invocation_id,
                    "reason": reason,
                },
            })
        except Exception:
            log.debug("Failed to send desktop tool_cancel for %s", invocation_id, exc_info=True)


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
