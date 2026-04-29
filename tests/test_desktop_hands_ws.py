# tests/test_desktop_hands_ws.py
from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.hands.desktop_manager import DesktopHandsManager
from agent.hands.desktop_executor import DesktopToolExecutor
from web.routers.desktop_hands import create_desktop_hands_router


class TestDesktopHandsWebSocket(unittest.TestCase):

    def setUp(self):
        self.manager = DesktopHandsManager()
        self.executor = DesktopToolExecutor(self.manager)
        router = create_desktop_hands_router(
            manager=self.manager,
            executor=self.executor,
            auth_fn=self._fake_auth,
        )
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)

    @staticmethod
    async def _fake_auth(token: str) -> dict | None:
        if token == "valid_token":
            return {"id": "user_1", "email": "test@test.com"}
        return None

    def test_connect_and_capabilities_exchange(self):
        with self.client.websocket_connect(
            "/ws/desktop-hands?token=valid_token"
        ) as ws:
            # Client sends capabilities
            ws.send_json({
                "type": "capabilities",
                "id": "msg_001",
                "timestamp": "2026-04-13T00:00:00Z",
                "payload": {
                    "client_version": "0.1.0",
                    "platform": "darwin-aarch64",
                    "paths": {
                        "home": "/Users/test",
                        "downloads": "/Users/test/Downloads",
                    },
                    "tools": [
                        {"name": "officecli", "version": "1.2.0", "operations": []},
                    ],
                },
            })

            # Server responds with ack
            ack = ws.receive_json()
            self.assertEqual(ack["type"], "capabilities_ack")
            self.assertIn("officecli", ack["payload"]["accepted"])

            # Verify manager has the connection registered
            self.assertTrue(self.manager.is_connected("user_1"))
            self.assertEqual(
                self.manager.get_path_aliases("user_1")["downloads"],
                "/Users/test/Downloads",
            )

    def test_reject_invalid_token(self):
        with self.assertRaises(Exception):
            with self.client.websocket_connect(
                "/ws/desktop-hands?token=bad_token"
            ) as ws:
                ws.receive_json()

    def test_tool_result_resolves_executor(self):
        """Client receives tool_call, sends back tool_result."""
        with self.client.websocket_connect(
            "/ws/desktop-hands?token=valid_token"
        ) as ws:
            # Handshake
            ws.send_json({
                "type": "capabilities",
                "id": "msg_001",
                "timestamp": "2026-04-13T00:00:00Z",
                "payload": {"tools": [{"name": "officecli", "operations": []}]},
            })
            ws.receive_json()  # ack

            # Client sends a tool_result (simulating response to a tool_call)
            ws.send_json({
                "type": "tool_result",
                "id": "msg_r1",
                "timestamp": "2026-04-13T00:00:00Z",
                "payload": {
                    "invocation_id": "inv_test",
                    "success": True,
                    "output": "done",
                    "artifacts": [],
                    "execution_time_ms": 100,
                },
            })

            # This should resolve without error (executor's resolve_invocation called)
