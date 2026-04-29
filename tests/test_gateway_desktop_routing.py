# tests/test_gateway_desktop_routing.py
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from agent.hands.protocol import ToolCall, ToolContext, ToolResult
from agent.hands.gateway import ToolGateway
from agent.hands.desktop_manager import DesktopHandsManager


class FakeSession:
    def __init__(self):
        self.emitted: list[tuple] = []

    async def emit_event(self, *args, **kwargs):
        self.emitted.append((args, kwargs))


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

    async def test_explicit_desktop_route_does_not_fallback_to_local(self):
        mgr = DesktopHandsManager()
        local = FakeLocalExecutor()
        desktop = FakeDesktopExecutor()
        gateway = ToolGateway(local=local, session=FakeSession(), desktop_manager=mgr, desktop_executor=desktop)
        gateway.set_route("list_dir", "desktop")

        call = ToolCall(tool_name="list_dir", params={"path": "~/Downloads"}, task_id="t1")
        ctx = ToolContext(user_id="user_1", task_id="t1")
        result = await gateway.execute(call, ctx)

        self.assertFalse(result.success)
        self.assertFalse(local.called)
        self.assertFalse(desktop.called)
        self.assertIn("desktop tool unavailable", result.error.lower())

    async def test_bind_deepagents_tools_includes_non_filesystem_desktop_tools(self):
        mgr = DesktopHandsManager()
        ws = FakeWebSocket()
        mgr.register(
            "user_1",
            ws,
            {
                "tools": [
                    {"name": "officecli", "operations": []},
                    {"name": "list_dir", "parameters": {"type": "object"}},
                    {"name": "shell", "parameters": {"type": "object"}},
                    {"name": "screenshot", "parameters": {"type": "object"}},
                    {"name": "sysinfo", "parameters": {"type": "object"}},
                ]
            },
        )

        gateway = ToolGateway(local=FakeLocalExecutor(), session=FakeSession(), desktop_manager=mgr, desktop_executor=FakeDesktopExecutor())
        ctx = ToolContext(user_id="user_1", task_id="t1")
        tools = gateway.bind_deepagents_tools("ppt", "t1", ctx)
        names = {tool.name for tool in tools}

        self.assertIn("officecli", names)
        self.assertIn("officecli_batch", names)
        self.assertIn("screenshot", names)
        self.assertIn("sysinfo", names)
        self.assertNotIn("list_dir", names)
        self.assertNotIn("shell", names)

    async def test_tool_events_include_stage_and_structured_result_metadata(self):
        mgr = DesktopHandsManager()
        ws = FakeWebSocket()
        mgr.register("user_1", ws, {"tools": [{"name": "officecli", "operations": []}]})

        session = FakeSession()
        local = FakeLocalExecutor()
        desktop = FakeDesktopExecutor()
        desktop.execute = AsyncMock(
            return_value=ToolResult(
                success=True,
                output='{"success": true, "command": "officecli create demo.pptx", "kind": "success", "message": "Created demo.pptx"}',
            )
        )
        gateway = ToolGateway(local=local, session=session, desktop_manager=mgr, desktop_executor=desktop)

        call = ToolCall(tool_name="officecli", params={"operation": "create", "_cost_stage": "build"}, task_id="t1")
        ctx = ToolContext(user_id="user_1", task_id="t1")
        result = await gateway.execute(call, ctx)

        self.assertTrue(result.success)
        started_payload = session.emitted[0][0][2]
        completed_payload = session.emitted[1][0][2]
        self.assertEqual(started_payload["stage"], "build")
        self.assertEqual(completed_payload["stage"], "build")
        self.assertEqual(completed_payload["command"], "officecli create demo.pptx")
        self.assertEqual(completed_payload["kind"], "success")
