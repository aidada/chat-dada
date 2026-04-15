from __future__ import annotations

import unittest

from agent.hands.deepagents_backend import build_deepagents_backend_factory
from agent.hands.desktop_manager import DesktopHandsManager
from agent.hands.gateway import ToolGateway
from agent.hands.protocol import ToolResult


class DummyRuntime:
    def __init__(self) -> None:
        self.state: dict[str, object] = {}


class FakeSession:
    async def emit_event(self, *args, **kwargs):
        return None


class FakeLocalExecutor:
    def __init__(self) -> None:
        self.called = False

    async def prepare(self, call, ctx):
        return None

    async def execute(self, call, ctx):
        self.called = True
        return ToolResult(success=False, output="", error=f"unexpected local call: {call.tool_name}")


class FakeDesktopExecutor:
    def __init__(self) -> None:
        self.called_tools: list[str] = []
        self.shell_success = True

    async def prepare(self, call, ctx):
        return None

    async def execute(self, call, ctx):
        self.called_tools.append(call.tool_name)
        if call.tool_name == "list_dir":
            path = call.params["path"]
            if path == "/":
                return ToolResult(success=True, output="dir: Applications\nfile: hosts")
            if path == "/Users/test/Downloads":
                return ToolResult(success=True, output="file: demo.txt")
            return ToolResult(success=True, output="")
        if call.tool_name == "file_read":
            return ToolResult(success=True, output="alpha\nbeta")
        if call.tool_name == "file_write":
            return ToolResult(success=True, output="10 bytes written")
        if call.tool_name == "file_edit":
            return ToolResult(success=True, output="replaced 1 occurrence")
        if call.tool_name == "file_search":
            base = call.params["path"].rstrip("/")
            return ToolResult(success=True, output=f"{base}/demo.txt")
        if call.tool_name == "grep":
            return ToolResult(success=True, output="1:alpha")
        if call.tool_name == "shell":
            if self.shell_success:
                return ToolResult(success=True, output="desktop shell ok")
            return ToolResult(success=False, output="", error="User denied permission")
        return ToolResult(success=False, output="", error=f"unexpected desktop tool: {call.tool_name}")


class FakeWebSocket:
    async def send_json(self, data):
        return None


class TestDeepagentsBackend(unittest.IsolatedAsyncioTestCase):
    def _make_gateway(self):
        manager = DesktopHandsManager()
        ws = FakeWebSocket()
        manager.register(
            "user_1",
            ws,
            {
                "paths": {
                    "home": "/Users/test",
                    "downloads": "/Users/test/Downloads",
                    "documents": "/Users/test/Documents",
                    "desktop": "/Users/test/Desktop",
                },
                "tools": [
                    {"name": "list_dir"},
                    {"name": "file_read"},
                    {"name": "file_write"},
                    {"name": "file_edit"},
                    {"name": "file_search"},
                    {"name": "grep"},
                    {"name": "shell"},
                ],
            },
        )
        local = FakeLocalExecutor()
        desktop = FakeDesktopExecutor()
        gateway = ToolGateway(local=local, session=FakeSession(), desktop_manager=manager, desktop_executor=desktop)
        return manager, gateway, local, desktop

    async def test_desktop_first_backend_exposes_aliases(self):
        manager, gateway, _local, _desktop = self._make_gateway()
        factory = build_deepagents_backend_factory(
            user_id="user_1",
            task_id="task_1",
            tool_gateway=gateway,
            desktop_manager=manager,
        )
        backend = factory(DummyRuntime())
        paths = [info["path"] for info in await backend.als_info("/")]
        self.assertIn("/downloads/", paths)
        self.assertIn("/workspace/", paths)
        self.assertIn("/scratch/", paths)

    async def test_workspace_route_uses_real_repo_view(self):
        manager, gateway, _local, _desktop = self._make_gateway()
        factory = build_deepagents_backend_factory(
            user_id="user_1",
            task_id="task_1",
            tool_gateway=gateway,
            desktop_manager=manager,
        )
        backend = factory(DummyRuntime())
        infos = await backend.aglob_info("*", path="/workspace")
        paths = [info["path"] for info in infos]
        self.assertTrue(any(path.startswith("/workspace/agent") or path.startswith("/workspace/tests") for path in paths))

    async def test_alias_directory_reads_desktop_view(self):
        manager, gateway, _local, desktop = self._make_gateway()
        factory = build_deepagents_backend_factory(
            user_id="user_1",
            task_id="task_1",
            tool_gateway=gateway,
            desktop_manager=manager,
        )
        backend = factory(DummyRuntime())
        infos = await backend.als_info("/downloads")
        self.assertEqual([info["path"] for info in infos], ["/downloads/demo.txt"])
        self.assertIn("list_dir", desktop.called_tools)

    async def test_shell_failure_does_not_fallback_to_workspace(self):
        manager, gateway, local, desktop = self._make_gateway()
        desktop.shell_success = False
        factory = build_deepagents_backend_factory(
            user_id="user_1",
            task_id="task_1",
            tool_gateway=gateway,
            desktop_manager=manager,
        )
        backend = factory(DummyRuntime())
        result = await backend.aexecute("echo hi")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("User denied permission", result.output)
        self.assertFalse(local.called)
