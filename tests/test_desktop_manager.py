# tests/test_desktop_manager.py
from __future__ import annotations

import asyncio
import unittest

from agent.hands.desktop_manager import DesktopHandsManager, DesktopConnection


class FakeWebSocket:
    """Minimal fake that records sent messages."""

    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = True


class TestDesktopHandsManager(unittest.IsolatedAsyncioTestCase):

    async def test_register_and_get(self):
        mgr = DesktopHandsManager()
        ws = FakeWebSocket()
        capabilities = {
            "client_version": "0.1.0",
            "platform": "darwin-aarch64",
            "paths": {
                "home": "/Users/test",
                "downloads": "/Users/test/Downloads",
            },
            "tools": [
                {
                    "name": "officecli",
                    "version": "1.2.0",
                    "operations": [
                        {"name": "create", "permission_level": "cautious"},
                    ],
                }
            ],
        }

        mgr.register("user_1", ws, capabilities)

        conn = mgr.get_connection("user_1")
        self.assertIsNotNone(conn)
        self.assertIs(conn.ws, ws)
        self.assertIn("officecli", conn.tool_names)
        self.assertEqual(conn.path_aliases["downloads"], "/Users/test/Downloads")

    async def test_get_returns_none_for_unknown_user(self):
        mgr = DesktopHandsManager()
        self.assertIsNone(mgr.get_connection("nobody"))

    async def test_unregister(self):
        mgr = DesktopHandsManager()
        ws = FakeWebSocket()
        mgr.register("user_1", ws, {"tools": []})
        mgr.unregister("user_1", ws)
        self.assertIsNone(mgr.get_connection("user_1"))

    async def test_has_tool(self):
        mgr = DesktopHandsManager()
        ws = FakeWebSocket()
        mgr.register("user_1", ws, {
            "tools": [{"name": "officecli", "operations": []}],
        })
        conn = mgr.get_connection("user_1")
        self.assertTrue(conn.has_tool("officecli"))
        self.assertFalse(conn.has_tool("ffmpeg"))

    async def test_can_query_tool_descriptor(self):
        mgr = DesktopHandsManager()
        ws = FakeWebSocket()
        mgr.register("user_1", ws, {
            "tools": [
                {
                    "name": "list_dir",
                    "description": "List directory contents",
                    "parameters": {"type": "object"},
                    "permission_level": "safe",
                }
            ],
        })
        descriptor = mgr.get_tool_descriptor("user_1", "list_dir")
        self.assertIsNotNone(descriptor)
        self.assertEqual(descriptor["permission_level"], "safe")
        self.assertEqual(len(mgr.list_tool_descriptors("user_1")), 1)

    async def test_can_query_path_aliases(self):
        mgr = DesktopHandsManager()
        ws = FakeWebSocket()
        mgr.register("user_1", ws, {
            "paths": {
                "home": "/Users/test",
                "downloads": "/Users/test/Downloads",
                "documents": "/Users/test/Documents",
            },
            "tools": [],
        })
        aliases = mgr.get_path_aliases("user_1")
        self.assertEqual(aliases["home"], "/Users/test")
        self.assertEqual(aliases["documents"], "/Users/test/Documents")

    async def test_stale_disconnect_does_not_remove_new_connection(self):
        mgr = DesktopHandsManager()
        ws_old = FakeWebSocket()
        ws_new = FakeWebSocket()

        mgr.register("user_1", ws_old, {"tools": [{"name": "officecli"}]})
        mgr.register("user_1", ws_new, {"tools": [{"name": "list_dir", "permission_level": "safe"}]})

        mgr.unregister("user_1", ws_old)

        conn = mgr.get_connection("user_1")
        self.assertIsNotNone(conn)
        assert conn is not None
        self.assertIs(conn.ws, ws_new)
        self.assertTrue(conn.has_tool("list_dir"))
