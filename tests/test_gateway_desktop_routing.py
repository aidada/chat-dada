# tests/test_gateway_desktop_routing.py
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from agent.hands.protocol import ToolCall, ToolContext, ToolResult
from agent.hands.gateway import ToolGateway
from agent.hands.desktop_manager import DesktopHandsManager


class FakeSession:
    async def emit_event(self, *args, **kwargs):
        pass


class FakeLocalExecutor:
    def __init__(self):
        self.called = False

    async def prepare(self, call, ctx):
        pass

    async def execute(self, call, ctx):
        self.called = True
        return ToolResult(success=True, output="local result")


class FakeDesktopExecutor:
    def __init__(self):
        self.called = False

    async def prepare(self, call, ctx):
        pass

    async def execute(self, call, ctx):
        self.called = True
        return ToolResult(success=True, output="desktop result")


class FakeWebSocket:
    async def send_json(self, data):
        pass


class TestGatewayDesktopRouting(unittest.IsolatedAsyncioTestCase):

    async def test_routes_to_desktop_when_connected(self):
        mgr = DesktopHandsManager()
        ws = FakeWebSocket()
        mgr.register("user_1", ws, {"tools": [{"name": "officecli", "operations": []}]})

        local = FakeLocalExecutor()
        desktop = FakeDesktopExecutor()
        gateway = ToolGateway(local=local, session=FakeSession(), desktop_manager=mgr, desktop_executor=desktop)

        call = ToolCall(tool_name="officecli", params={"operation": "create"}, task_id="t1")
        ctx = ToolContext(user_id="user_1", task_id="t1")
        result = await gateway.execute(call, ctx)

        self.assertTrue(desktop.called)
        self.assertFalse(local.called)
        self.assertEqual(result.output, "desktop result")

    async def test_falls_back_to_local_when_not_connected(self):
        mgr = DesktopHandsManager()
        local = FakeLocalExecutor()
        desktop = FakeDesktopExecutor()
        gateway = ToolGateway(local=local, session=FakeSession(), desktop_manager=mgr, desktop_executor=desktop)

        call = ToolCall(tool_name="officecli", params={"operation": "create"}, task_id="t1")
        ctx = ToolContext(user_id="user_1", task_id="t1")
        result = await gateway.execute(call, ctx)

        self.assertTrue(local.called)
        self.assertFalse(desktop.called)
        self.assertEqual(result.output, "local result")

    async def test_falls_back_when_tool_not_in_capabilities(self):
        mgr = DesktopHandsManager()
        ws = FakeWebSocket()
        # Client only has "ffmpeg", not "officecli"
        mgr.register("user_1", ws, {"tools": [{"name": "ffmpeg", "operations": []}]})

        local = FakeLocalExecutor()
        desktop = FakeDesktopExecutor()
        gateway = ToolGateway(local=local, session=FakeSession(), desktop_manager=mgr, desktop_executor=desktop)

        call = ToolCall(tool_name="officecli", params={}, task_id="t1")
        ctx = ToolContext(user_id="user_1", task_id="t1")
        result = await gateway.execute(call, ctx)

        self.assertTrue(local.called)
        self.assertFalse(desktop.called)

    async def test_no_desktop_manager_uses_local(self):
        """Backward compatible -- no desktop_manager means always local."""
        local = FakeLocalExecutor()
        gateway = ToolGateway(local=local, session=FakeSession())

        call = ToolCall(tool_name="officecli", params={}, task_id="t1")
        ctx = ToolContext(user_id="user_1", task_id="t1")
        result = await gateway.execute(call, ctx)

        self.assertTrue(local.called)
