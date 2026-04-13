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

    async def test_get_returns_none_for_unknown_user(self):
        mgr = DesktopHandsManager()
        self.assertIsNone(mgr.get_connection("nobody"))

    async def test_unregister(self):
        mgr = DesktopHandsManager()
        ws = FakeWebSocket()
        mgr.register("user_1", ws, {"tools": []})
        mgr.unregister("user_1")
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
