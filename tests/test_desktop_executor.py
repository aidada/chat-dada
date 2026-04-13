# tests/test_desktop_executor.py
from __future__ import annotations

import asyncio
import unittest

from agent.hands.protocol import ToolCall, ToolContext, ToolResult
from agent.hands.desktop_executor import DesktopToolExecutor
from agent.hands.desktop_manager import DesktopHandsManager


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self._response: dict | None = None

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    def set_response(self, response: dict) -> None:
        self._response = response


class TestDesktopToolExecutor(unittest.IsolatedAsyncioTestCase):

    async def test_execute_sends_tool_call_and_returns_result(self):
        mgr = DesktopHandsManager()
        executor = DesktopToolExecutor(mgr)
        ws = FakeWebSocket()
        mgr.register("user_1", ws, {
            "tools": [{"name": "officecli", "operations": []}],
        })

        call = ToolCall(
            tool_name="officecli",
            params={"operation": "create", "filename": "test.pptx"},
            task_id="task_1",
        )
        ctx = ToolContext(user_id="user_1", task_id="task_1")

        # Simulate: client sends result back after receiving tool_call
        async def simulate_client_response():
            # Wait for the tool_call to be sent
            while not ws.sent:
                await asyncio.sleep(0.01)
            inv_id = ws.sent[0]["payload"]["invocation_id"]
            executor.resolve_invocation(inv_id, {
                "success": True,
                "output": "Created test.pptx",
                "artifacts": [{"type": "local_file", "path": "/tmp/test.pptx"}],
                "execution_time_ms": 500,
            })

        task = asyncio.create_task(simulate_client_response())
        result = await executor.execute(call, ctx)
        await task

        self.assertTrue(result.success)
        self.assertEqual(result.output, "Created test.pptx")
        self.assertEqual(len(result.artifacts), 1)
        self.assertEqual(result.artifacts[0]["type"], "local_file")

        # Verify the WS message format
        msg = ws.sent[0]
        self.assertEqual(msg["type"], "tool_call")
        self.assertEqual(msg["payload"]["tool"], "officecli")
        self.assertEqual(msg["payload"]["operation"], "create")

    async def test_execute_returns_error_when_no_connection(self):
        mgr = DesktopHandsManager()
        executor = DesktopToolExecutor(mgr)

        call = ToolCall(tool_name="officecli", params={}, task_id="task_1")
        ctx = ToolContext(user_id="user_1", task_id="task_1")

        result = await executor.execute(call, ctx)

        self.assertFalse(result.success)
        self.assertIn("no desktop connection", result.error.lower())

    async def test_execute_timeout(self):
        mgr = DesktopHandsManager()
        executor = DesktopToolExecutor(mgr, default_timeout_s=0.1)
        ws = FakeWebSocket()
        mgr.register("user_1", ws, {"tools": [{"name": "officecli", "operations": []}]})

        call = ToolCall(tool_name="officecli", params={"operation": "create"}, task_id="t1", timeout_ms=100)
        ctx = ToolContext(user_id="user_1", task_id="t1")

        result = await executor.execute(call, ctx)

        self.assertFalse(result.success)
        self.assertIn("timeout", result.error.lower())
