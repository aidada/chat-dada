# Desktop Hands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable the Tauri desktop client to act as a Hands execution endpoint, receiving structured OfficeCLI tool calls from the server via WebSocket and generating files locally on the user's machine.

**Architecture:** A dedicated WebSocket channel (`/ws/desktop-hands`) connects the Tauri client to the server. The client reports its available tools on connect. The server's `ToolGateway` routes eligible tool calls to a new `DesktopToolExecutor` when a desktop connection is available, falling back to the existing `LocalToolExecutor` otherwise. The client converts structured operation parameters (create/add/set/...) into OfficeCLI CLI commands internally — the model never sees raw command strings.

**Tech Stack:** Python/FastAPI WebSocket (backend), TypeScript + Tauri sidecar (frontend), existing ToolExecutor protocol, existing Tauri Tool trait system.

**Spec:** `docs/superpowers/specs/2026-04-13-desktop-hands-design.md`

---

## File Structure

### Backend (chat-dada) — New Files

| File | Responsibility |
|:---|:---|
| `agent/hands/desktop_manager.py` | Connection registry: maps user_id → WebSocket + capabilities cache. Pure async logic, no FastAPI dependency. |
| `agent/hands/desktop_executor.py` | Implements `ToolExecutor` protocol. Serializes `ToolCall` → WS message, awaits `tool_result`, deserializes back to `ToolResult`. |
| `web/routers/desktop_hands.py` | FastAPI WebSocket endpoint `/ws/desktop-hands`. Auth, message dispatch, lifecycle. |
| `tests/test_desktop_manager.py` | Unit tests for DesktopHandsManager. |
| `tests/test_desktop_executor.py` | Unit tests for DesktopToolExecutor. |
| `tests/test_desktop_hands_ws.py` | Integration tests for WebSocket endpoint. |

### Backend (chat-dada) — Modified Files

| File | Change |
|:---|:---|
| `agent/hands/protocol.py` | No change needed — `ToolResult.artifacts` already accepts `list[dict[str, Any]]`, `local_file` is just a dict shape convention. |
| `agent/hands/gateway.py` | Add `desktop` routing branch: check `DesktopHandsManager` for active connection before falling back to local. |
| `agent/hands/__init__.py` | Re-export `DesktopHandsManager`, `DesktopToolExecutor`. |
| `web/app.py` | Register `desktop_hands_router`. |

### Frontend (chat-dada-front) — New Files

| File | Responsibility |
|:---|:---|
| `src/desktop-hands/types.ts` | TypeScript types for all WS message types (capabilities, tool_call, tool_result, etc.). |
| `src/desktop-hands/officecli.ts` | Maps structured operation params → OfficeCLI CLI args. Calls Tauri `invoke_tool("shell", ...)`. |
| `src/desktop-hands/client.ts` | WebSocket connection manager: connect, auth, capabilities report, message dispatch, reconnect. |

### Frontend (chat-dada-front) — Modified Files

| File | Change |
|:---|:---|
| `src/components/chat/TaskCard.tsx` | Handle `artifact.type === "local_file"` — show local path + "Open File" / "Open Folder" buttons via Tauri shell.open(). |
| `src/App.tsx` | Initialize `DesktopHandsClient` after auth, teardown on logout. |

---

## Task 1: DesktopHandsManager — Connection & Capabilities Registry

**Files:**
- Create: `agent/hands/desktop_manager.py`
- Test: `tests/test_desktop_manager.py`

- [ ] **Step 1: Write the failing test — register and retrieve a connection**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_desktop_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.hands.desktop_manager'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/hands/desktop_manager.py
"""DesktopHandsManager — 桌面端连接注册与能力缓存。

管理所有活跃的桌面端 WebSocket 连接，缓存每个客户端上报的工具能力列表。
纯 async 逻辑，不依赖 FastAPI。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("chatdada.hands.desktop")


class WebSocketLike(Protocol):
    """Minimal WebSocket interface for testability."""
    async def send_json(self, data: dict) -> None: ...
    async def close(self, code: int = 1000) -> None: ...


@dataclass
class DesktopConnection:
    """A single desktop client connection with its capabilities."""

    ws: Any  # WebSocketLike at runtime
    capabilities: dict[str, Any]
    tool_names: set[str] = field(default_factory=set)

    def has_tool(self, name: str) -> bool:
        return name in self.tool_names


class DesktopHandsManager:
    """Registry of active desktop client connections."""

    def __init__(self) -> None:
        self._connections: dict[str, DesktopConnection] = {}

    def register(
        self,
        user_id: str,
        ws: Any,
        capabilities: dict[str, Any],
    ) -> DesktopConnection:
        tools = capabilities.get("tools", [])
        tool_names = {t["name"] for t in tools if "name" in t}
        conn = DesktopConnection(
            ws=ws,
            capabilities=capabilities,
            tool_names=tool_names,
        )
        self._connections[user_id] = conn
        log.info(
            "Desktop connected: user=%s tools=%s",
            user_id,
            sorted(tool_names),
        )
        return conn

    def unregister(self, user_id: str) -> None:
        removed = self._connections.pop(user_id, None)
        if removed:
            log.info("Desktop disconnected: user=%s", user_id)

    def get_connection(self, user_id: str) -> DesktopConnection | None:
        return self._connections.get(user_id)

    def is_connected(self, user_id: str) -> bool:
        return user_id in self._connections
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_desktop_manager.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add agent/hands/desktop_manager.py tests/test_desktop_manager.py
git commit -m "feat(hands): add DesktopHandsManager for desktop connection registry"
```

---

## Task 2: DesktopToolExecutor — Remote Execution via WebSocket

**Files:**
- Create: `agent/hands/desktop_executor.py`
- Test: `tests/test_desktop_executor.py`

- [ ] **Step 1: Write the failing test — execute routes call through WebSocket**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_desktop_executor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.hands.desktop_executor'`

- [ ] **Step 3: Write minimal implementation**

```python
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

    async def prepare(self, call: ToolCall, ctx: ToolContext) -> None:
        """No-op — permission checks happen on the client side."""
        return None

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
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

            return ToolResult(
                success=result_payload.get("success", False),
                output=result_payload.get("output", ""),
                artifacts=result_payload.get("artifacts", []),
                error=result_payload.get("error"),
                execution_time_ms=result_payload.get("execution_time_ms", elapsed_ms),
            )

        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            log.warning(
                "Desktop tool call timed out: tool=%s inv=%s timeout=%ss",
                call.tool_name, invocation_id, timeout_s,
            )
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


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_desktop_executor.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add agent/hands/desktop_executor.py tests/test_desktop_executor.py
git commit -m "feat(hands): add DesktopToolExecutor for WebSocket-based tool execution"
```

---

## Task 3: ToolGateway Desktop Routing

**Files:**
- Modify: `agent/hands/gateway.py:45-58`
- Modify: `agent/hands/__init__.py`
- Test: `tests/test_gateway_desktop_routing.py`

- [ ] **Step 1: Write the failing test — gateway routes to desktop when available**

```python
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
        """Backward compatible — no desktop_manager means always local."""
        local = FakeLocalExecutor()
        gateway = ToolGateway(local=local, session=FakeSession())

        call = ToolCall(tool_name="officecli", params={}, task_id="t1")
        ctx = ToolContext(user_id="user_1", task_id="t1")
        result = await gateway.execute(call, ctx)

        self.assertTrue(local.called)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_gateway_desktop_routing.py -v`
Expected: FAIL — `TypeError: ToolGateway.__init__() got an unexpected keyword argument 'desktop_manager'`

- [ ] **Step 3: Modify ToolGateway to support desktop routing**

In `agent/hands/gateway.py`, update `__init__` and `execute`:

```python
# agent/hands/gateway.py — updated __init__ signature (line 29-39 replacement)
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
        self._routing: dict[str, str] = {}
        self._desktop_manager = desktop_manager
        self._desktop_executor = desktop_executor
```

Update the executor selection in `execute()` (line 57-58 replacement):

```python
        # Desktop routing: if user has an active desktop connection with this tool
        target = self._routing.get(call.tool_name, "local")
        executor = self._local  # default

        if (
            self._desktop_manager is not None
            and self._desktop_executor is not None
        ):
            conn = self._desktop_manager.get_connection(ctx.user_id)
            if conn is not None and conn.has_tool(call.tool_name):
                executor = self._desktop_executor
                target = "desktop"

        if target == "local" and self._routing.get(call.tool_name) == "remote" and self._remote is not None:
            executor = self._remote
            target = "remote"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_gateway_desktop_routing.py -v`
Expected: 4 passed

- [ ] **Step 5: Run existing gateway tests to verify no regression**

Run: `.venv/bin/python -m pytest tests/test_platform_refactor.py -v`
Expected: All existing tests still pass

- [ ] **Step 6: Update `__init__.py` re-exports**

In `agent/hands/__init__.py`, add:

```python
from agent.hands.desktop_manager import DesktopHandsManager, DesktopConnection
from agent.hands.desktop_executor import DesktopToolExecutor
```

- [ ] **Step 7: Commit**

```bash
git add agent/hands/gateway.py agent/hands/__init__.py tests/test_gateway_desktop_routing.py
git commit -m "feat(hands): add desktop routing to ToolGateway with fallback to local"
```

---

## Task 4: WebSocket Endpoint

**Files:**
- Create: `web/routers/desktop_hands.py`
- Modify: `web/app.py:16-17,56-61`
- Test: `tests/test_desktop_hands_ws.py`

- [ ] **Step 1: Write the failing test — WebSocket connects and exchanges capabilities**

```python
# tests/test_desktop_hands_ws.py
from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocket

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

    def test_reject_invalid_token(self):
        with self.assertRaises(Exception):
            with self.client.websocket_connect(
                "/ws/desktop-hands?token=bad_token"
            ) as ws:
                ws.receive_json()

    def test_tool_result_resolves_executor(self):
        """Client receives tool_call, sends back tool_result."""
        import asyncio

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_desktop_hands_ws.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'web.routers.desktop_hands'`

- [ ] **Step 3: Write the WebSocket endpoint**

```python
# web/routers/desktop_hands.py
"""WebSocket endpoint for Desktop Hands — /ws/desktop-hands.

Handles:
- Authentication via query param token
- Capabilities handshake
- Tool result forwarding to DesktopToolExecutor
- Connection lifecycle (register on connect, unregister on disconnect)
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from agent.hands.desktop_executor import DesktopToolExecutor
from agent.hands.desktop_manager import DesktopHandsManager

log = logging.getLogger("chatdada.web.desktop_hands")


def create_desktop_hands_router(
    *,
    manager: DesktopHandsManager,
    executor: DesktopToolExecutor,
    auth_fn: Callable[[str], Awaitable[dict | None]],
) -> APIRouter:
    """Factory that creates the router with injected dependencies."""

    router = APIRouter()

    @router.websocket("/ws/desktop-hands")
    async def desktop_hands_ws(
        ws: WebSocket,
        token: str = Query(""),
    ) -> None:
        # 1. Authenticate
        user = await auth_fn(token)
        if user is None:
            await ws.close(code=4001)
            return

        await ws.accept()
        user_id = user["id"]
        log.info("Desktop WS accepted: user=%s", user_id)

        try:
            # 2. Wait for capabilities message
            caps_msg = await ws.receive_json()
            if caps_msg.get("type") != "capabilities":
                await ws.send_json({"type": "error", "payload": {"message": "Expected capabilities message"}})
                await ws.close(code=4002)
                return

            payload = caps_msg.get("payload", {})
            manager.register(user_id, ws, payload)

            # 3. Send ack
            tool_names = [t["name"] for t in payload.get("tools", []) if "name" in t]
            await ws.send_json({
                "type": "capabilities_ack",
                "id": f"ack_{caps_msg.get('id', '')}",
                "payload": {
                    "accepted": tool_names,
                    "rejected": [],
                },
            })

            # 4. Message loop
            while True:
                msg = await ws.receive_json()
                msg_type = msg.get("type", "")
                msg_payload = msg.get("payload", {})

                if msg_type == "tool_result":
                    inv_id = msg_payload.get("invocation_id", "")
                    executor.resolve_invocation(inv_id, msg_payload)

                elif msg_type == "tool_progress":
                    # Forward progress — currently logged, future: emit as transient event
                    log.debug(
                        "Desktop progress: inv=%s progress=%.1f msg=%s",
                        msg_payload.get("invocation_id"),
                        msg_payload.get("progress", 0),
                        msg_payload.get("message", ""),
                    )

                elif msg_type == "pong":
                    pass  # heartbeat response

                else:
                    log.warning("Unknown desktop message type: %s", msg_type)

        except WebSocketDisconnect:
            log.info("Desktop WS disconnected: user=%s", user_id)
        except Exception:
            log.exception("Desktop WS error: user=%s", user_id)
        finally:
            manager.unregister(user_id)

    return router
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_desktop_hands_ws.py -v`
Expected: 3 passed

- [ ] **Step 5: Register router in web/app.py**

Add import at top of `web/app.py`:

```python
from web.routers.desktop_hands import create_desktop_hands_router
```

Add after the existing `app.include_router(system_router)` line:

```python
# Desktop Hands WebSocket — requires runtime wiring
from agent.hands.desktop_manager import DesktopHandsManager
from agent.hands.desktop_executor import DesktopToolExecutor

_desktop_manager = DesktopHandsManager()
_desktop_executor = DesktopToolExecutor(_desktop_manager)

async def _ws_auth(token: str) -> dict | None:
    from infra.db.session import SessionFactory
    from domain.auth.services import AuthService
    async with SessionFactory() as session:
        auth_service = AuthService(session)
        user, _ = await auth_service.get_user_by_session_token(token)
    return {"id": user.id, "email": user.email} if user else None

app.include_router(create_desktop_hands_router(
    manager=_desktop_manager,
    executor=_desktop_executor,
    auth_fn=_ws_auth,
))
```

- [ ] **Step 6: Commit**

```bash
git add web/routers/desktop_hands.py web/app.py tests/test_desktop_hands_ws.py
git commit -m "feat(web): add /ws/desktop-hands WebSocket endpoint"
```

---

## Task 5: Frontend — Protocol Types

**Files:**
- Create: `src/desktop-hands/types.ts`

- [ ] **Step 1: Create the types file**

```typescript
// src/desktop-hands/types.ts

/** Envelope for all Desktop Hands WebSocket messages. */
export interface DHMessage<T extends string = string, P = unknown> {
  type: T;
  id: string;
  timestamp: string;
  payload: P;
}

// ── Client → Server ─────────────────────────────────────────────

export interface ToolOperation {
  name: string;
  description?: string;
  parameters?: Record<string, unknown>;
  permission_level: "safe" | "cautious" | "dangerous";
}

export interface ToolDescriptor {
  name: string;
  version?: string;
  operations: ToolOperation[];
}

export type CapabilitiesPayload = {
  client_version: string;
  platform: string;
  tools: ToolDescriptor[];
};

export type ToolResultPayload = {
  invocation_id: string;
  success: boolean;
  output: string;
  error?: string;
  artifacts: LocalArtifact[];
  execution_time_ms: number;
};

export type ToolProgressPayload = {
  invocation_id: string;
  progress: number; // 0-1
  message: string;
};

// ── Server → Client ─────────────────────────────────────────────

export type CapabilitiesAckPayload = {
  accepted: string[];
  rejected: string[];
};

export type ToolCallPayload = {
  invocation_id: string;
  task_id: string;
  tool: string;
  operation: string;
  params: Record<string, unknown>;
  timeout_ms: number;
};

export type ToolCancelPayload = {
  invocation_id: string;
  reason: string;
};

// ── Artifacts ────────────────────────────────────────────────────

export interface LocalArtifact {
  type: "local_file";
  name: string;
  path: string;
  mime?: string;
  size_bytes?: number;
}

// ── Message type aliases ─────────────────────────────────────────

export type CapabilitiesMsg = DHMessage<"capabilities", CapabilitiesPayload>;
export type CapabilitiesAckMsg = DHMessage<"capabilities_ack", CapabilitiesAckPayload>;
export type ToolCallMsg = DHMessage<"tool_call", ToolCallPayload>;
export type ToolResultMsg = DHMessage<"tool_result", ToolResultPayload>;
export type ToolProgressMsg = DHMessage<"tool_progress", ToolProgressPayload>;
export type ToolCancelMsg = DHMessage<"tool_cancel", ToolCancelPayload>;
export type PingMsg = DHMessage<"ping", Record<string, never>>;
export type PongMsg = DHMessage<"pong", Record<string, never>>;

export type ServerMessage = CapabilitiesAckMsg | ToolCallMsg | ToolCancelMsg | PingMsg;
export type ClientMessage = CapabilitiesMsg | ToolResultMsg | ToolProgressMsg | PongMsg;
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada-front && npx tsc --noEmit src/desktop-hands/types.ts`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
cd /Users/luozhongxu/Workspaces/chat-dada-front
git add src/desktop-hands/types.ts
git commit -m "feat(desktop-hands): add TypeScript protocol types"
```

---

## Task 6: Frontend — OfficeCLI Adapter

**Files:**
- Create: `src/desktop-hands/officecli.ts`

- [ ] **Step 1: Create the adapter**

This module converts structured operation params into OfficeCLI CLI arguments and executes via the Tauri shell tool.

```typescript
// src/desktop-hands/officecli.ts
import { invoke } from "@tauri-apps/api/core";
import type { ToolCallPayload, ToolResultPayload, LocalArtifact } from "./types";

/**
 * Execute a structured OfficeCLI operation via the Tauri shell tool.
 *
 * Converts structured params (operation + params) into CLI arguments.
 * The model never sees raw command strings — this is the only place
 * where structured operations become CLI commands.
 */
export async function executeOfficeCLI(
  call: ToolCallPayload,
  outputDir: string,
): Promise<ToolResultPayload> {
  const start = performance.now();
  try {
    const args = buildCliArgs(call.operation, call.params, outputDir);
    const result = await invoke<{ success: boolean; output: string }>(
      "invoke_tool",
      {
        name: "shell",
        params: {
          command: `officecli ${args.join(" ")}`,
          timeout_ms: call.timeout_ms || 30000,
        },
      },
    );

    const elapsed = Math.round(performance.now() - start);
    const artifacts = extractArtifacts(call, outputDir, result.output);

    return {
      invocation_id: call.invocation_id,
      success: result.success,
      output: result.output,
      artifacts,
      execution_time_ms: elapsed,
    };
  } catch (err) {
    const elapsed = Math.round(performance.now() - start);
    return {
      invocation_id: call.invocation_id,
      success: false,
      output: "",
      error: err instanceof Error ? err.message : String(err),
      artifacts: [],
      execution_time_ms: elapsed,
    };
  }
}

/**
 * Map structured operation + params → CLI argument array.
 * Each operation has a well-defined set of allowed params.
 */
function buildCliArgs(
  operation: string,
  params: Record<string, unknown>,
  outputDir: string,
): string[] {
  switch (operation) {
    case "create": {
      const filename = params.filename as string;
      const filepath = `${outputDir}/${filename}`;
      return ["create", filepath];
    }
    case "add": {
      const target = params.target as string;
      const contentType = params.type as string;
      const args = ["add", target, "--type", contentType];
      if (params.content) args.push("--content", JSON.stringify(params.content));
      if (params.position !== undefined) args.push("--position", String(params.position));
      return args;
    }
    case "set": {
      const target = params.target as string;
      const property = params.property as string;
      const value = params.value as string;
      return ["set", target, "--property", property, "--value", value];
    }
    case "get": {
      const target = params.target as string;
      const args = ["get", target];
      if (params.property) args.push("--property", params.property as string);
      return args;
    }
    case "query": {
      const target = params.target as string;
      const args = ["query", target];
      if (params.filter) args.push("--filter", params.filter as string);
      return args;
    }
    case "validate": {
      const target = params.target as string;
      return ["validate", target];
    }
    case "batch": {
      const ops = params.operations as Array<{ operation: string; params: Record<string, unknown> }>;
      // Batch uses JSON stdin
      const batchJson = JSON.stringify(
        ops.map((op) => ({ command: op.operation, ...op.params })),
      );
      return ["batch", "--json", batchJson];
    }
    default:
      // Pass through unknown operations as-is (forward compatible)
      return [operation, ...Object.values(params).map(String)];
  }
}

/** Extract local file artifacts from command output. */
function extractArtifacts(
  call: ToolCallPayload,
  outputDir: string,
  output: string,
): LocalArtifact[] {
  // For create operations, the output file is known
  if (call.operation === "create" && call.params.filename) {
    const filename = call.params.filename as string;
    const ext = filename.split(".").pop() || "";
    const mimeMap: Record<string, string> = {
      pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    };
    return [
      {
        type: "local_file",
        name: filename,
        path: `${outputDir}/${filename}`,
        mime: mimeMap[ext],
      },
    ];
  }
  return [];
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada-front && npx tsc --noEmit src/desktop-hands/officecli.ts`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
cd /Users/luozhongxu/Workspaces/chat-dada-front
git add src/desktop-hands/officecli.ts
git commit -m "feat(desktop-hands): add OfficeCLI structured operation adapter"
```

---

## Task 7: Frontend — DesktopHandsClient (WebSocket)

**Files:**
- Create: `src/desktop-hands/client.ts`

- [ ] **Step 1: Create the WebSocket client**

```typescript
// src/desktop-hands/client.ts
import type {
  ServerMessage,
  CapabilitiesPayload,
  ToolCallPayload,
  ToolResultPayload,
} from "./types";
import { executeOfficeCLI } from "./officecli";

const HEARTBEAT_INTERVAL_MS = 30_000;
const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 16000, 30000];

type ClientState = "disconnected" | "connecting" | "connected";

export class DesktopHandsClient {
  private ws: WebSocket | null = null;
  private state: ClientState = "disconnected";
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectAttempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private destroyed = false;

  constructor(
    private serverUrl: string,
    private token: string,
    private outputDir: string,
  ) {}

  /** Start connection. Call after user logs in. */
  connect(): void {
    if (this.destroyed || this.state !== "disconnected") return;
    this.state = "connecting";

    const url = `${this.serverUrl.replace(/^http/, "ws")}/ws/desktop-hands?token=${this.token}`;
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this.state = "connected";
      this.reconnectAttempt = 0;
      this.sendCapabilities();
      this.startHeartbeat();
    };

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data) as ServerMessage;
        this.handleMessage(msg);
      } catch {
        console.warn("[DesktopHands] Failed to parse message");
      }
    };

    this.ws.onclose = () => {
      this.cleanup();
      if (!this.destroyed) this.scheduleReconnect();
    };

    this.ws.onerror = () => {
      // onclose will fire after onerror
    };
  }

  /** Graceful shutdown. Call on logout or app close. */
  destroy(): void {
    this.destroyed = true;
    this.cleanup();
    this.ws?.close(1000);
    this.ws = null;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  /** Update output directory (from settings). */
  setOutputDir(dir: string): void {
    this.outputDir = dir;
  }

  // ── Private ────────────────────────────────────────────────

  private sendCapabilities(): void {
    // Discover locally available tools
    const capabilities: CapabilitiesPayload = {
      client_version: "0.1.0",
      platform: detectPlatform(),
      tools: [
        {
          name: "officecli",
          operations: [
            { name: "create", permission_level: "cautious" },
            { name: "add", permission_level: "cautious" },
            { name: "set", permission_level: "cautious" },
            { name: "get", permission_level: "safe" },
            { name: "query", permission_level: "safe" },
            { name: "validate", permission_level: "safe" },
            { name: "batch", permission_level: "cautious" },
            { name: "watch", permission_level: "safe" },
          ],
        },
      ],
    };

    this.send({
      type: "capabilities",
      id: `msg_caps_${Date.now()}`,
      timestamp: new Date().toISOString(),
      payload: capabilities,
    });
  }

  private handleMessage(msg: ServerMessage): void {
    switch (msg.type) {
      case "capabilities_ack":
        console.log("[DesktopHands] Capabilities accepted:", msg.payload.accepted);
        break;

      case "tool_call":
        this.handleToolCall(msg.payload);
        break;

      case "tool_cancel":
        console.warn("[DesktopHands] Tool cancelled:", msg.payload.invocation_id);
        // TODO: cancel in-progress execution
        break;

      case "ping":
        this.send({ type: "pong", id: `pong_${Date.now()}`, timestamp: new Date().toISOString(), payload: {} });
        break;

      default:
        console.warn("[DesktopHands] Unknown message type:", (msg as { type: string }).type);
    }
  }

  private async handleToolCall(call: ToolCallPayload): Promise<void> {
    let result: ToolResultPayload;

    if (call.tool === "officecli") {
      result = await executeOfficeCLI(call, this.outputDir);
    } else {
      result = {
        invocation_id: call.invocation_id,
        success: false,
        output: "",
        error: `Unknown tool: ${call.tool}`,
        artifacts: [],
        execution_time_ms: 0,
      };
    }

    this.send({
      type: "tool_result",
      id: `msg_result_${Date.now()}`,
      timestamp: new Date().toISOString(),
      payload: result,
    });
  }

  private send(msg: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  private startHeartbeat(): void {
    this.heartbeatTimer = setInterval(() => {
      this.send({ type: "ping", id: `ping_${Date.now()}`, timestamp: new Date().toISOString(), payload: {} });
    }, HEARTBEAT_INTERVAL_MS);
  }

  private cleanup(): void {
    this.state = "disconnected";
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private scheduleReconnect(): void {
    const delay = RECONNECT_DELAYS[Math.min(this.reconnectAttempt, RECONNECT_DELAYS.length - 1)];
    this.reconnectAttempt++;
    console.log(`[DesktopHands] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempt})`);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }
}

function detectPlatform(): string {
  const ua = navigator.userAgent.toLowerCase();
  if (ua.includes("mac")) return "darwin";
  if (ua.includes("win")) return "windows";
  return "linux";
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada-front && npx tsc --noEmit src/desktop-hands/client.ts`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
cd /Users/luozhongxu/Workspaces/chat-dada-front
git add src/desktop-hands/client.ts
git commit -m "feat(desktop-hands): add DesktopHandsClient WebSocket manager"
```

---

## Task 8: Frontend — TaskCard Local File Artifact

**Files:**
- Modify: `src/components/chat/TaskCard.tsx:187-213`

- [ ] **Step 1: Add local file artifact rendering**

Replace the existing artifacts section (lines 187-213) in `TaskCard.tsx`. The change adds detection of `artifact.type === "local_file"` and renders "Open File" / "Open Folder" buttons using the Tauri shell API instead of a download link.

```tsx
              {/* Artifacts */}
              {card.artifacts.length > 0 && (
                <div style={{ borderTop: "1px solid var(--separator)", paddingTop: 12 }}>
                  <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--text-tri)", marginBottom: 8 }}>
                    最终交付
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {card.artifacts.map((artifact, i) => {
                      const isLocal = artifact.type === "local_file";
                      return (
                        <div
                          key={`${artifact.name || artifact.path}-${i}`}
                          style={{
                            display: "flex", alignItems: "center", justifyContent: "space-between",
                            padding: "10px 12px", borderRadius: 10,
                            background: "var(--elevated)", border: "1px solid var(--separator)",
                          }}
                        >
                          <div>
                            <div style={{ fontSize: 13, fontWeight: 500, color: "var(--text)" }}>{artifact.name || artifact.path || "artifact"}</div>
                            <div style={{ fontSize: 11, color: "var(--text-tri)" }}>
                              {isLocal ? artifact.path : (artifact.type || "file")}
                            </div>
                          </div>
                          {isLocal ? (
                            <div style={{ display: "flex", gap: 6 }}>
                              <button
                                type="button"
                                onClick={() => openLocalFile(artifact.path)}
                                style={{
                                  padding: "4px 10px", borderRadius: 6, fontSize: 12,
                                  border: "1px solid var(--separator)", background: "var(--surface)",
                                  color: "var(--text)", cursor: "pointer",
                                }}
                              >
                                打开文件
                              </button>
                              <button
                                type="button"
                                onClick={() => openLocalFolder(artifact.path)}
                                style={{
                                  padding: "4px 10px", borderRadius: 6, fontSize: 12,
                                  border: "1px solid var(--separator)", background: "var(--surface)",
                                  color: "var(--text-sec)", cursor: "pointer",
                                }}
                              >
                                打开文件夹
                              </button>
                            </div>
                          ) : (
                            <a
                              href={artifact.url || artifact.path || "#"}
                              target="_blank"
                              rel="noreferrer"
                              style={{ color: "var(--text-tri)", textDecoration: "none" }}
                            >
                              <ArrowUpRight size={14} strokeWidth={1.5} />
                            </a>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
```

Add helper functions at the top of the file (after imports):

```typescript
async function openLocalFile(path: string): Promise<void> {
  try {
    const { open } = await import("@tauri-apps/plugin-shell");
    await open(path);
  } catch {
    // Fallback for web: do nothing (local files not accessible)
    console.warn("Cannot open local file in web mode");
  }
}

async function openLocalFolder(filePath: string): Promise<void> {
  try {
    const { open } = await import("@tauri-apps/plugin-shell");
    const folder = filePath.substring(0, filePath.lastIndexOf("/"));
    await open(folder);
  } catch {
    console.warn("Cannot open local folder in web mode");
  }
}
```

- [ ] **Step 2: Verify build**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada-front && npm run build`
Expected: Build succeeds

- [ ] **Step 3: Commit**

```bash
cd /Users/luozhongxu/Workspaces/chat-dada-front
git add src/components/chat/TaskCard.tsx
git commit -m "feat(ui): support local_file artifacts with open file/folder buttons"
```

---

## Task 9: Frontend — Initialize Client in App.tsx

**Files:**
- Modify: `src/App.tsx`

- [ ] **Step 1: Add DesktopHandsClient initialization**

Add import at top of `App.tsx`:

```typescript
import { DesktopHandsClient } from "./desktop-hands/client";
```

Add a ref to hold the client instance. Inside the `App` component, add after existing state declarations:

```typescript
const desktopHandsRef = useRef<DesktopHandsClient | null>(null);
```

Add an effect that starts the client when the user is authenticated and stops on logout. Place it after the existing auth-related effects:

```typescript
// Desktop Hands — connect when authenticated, destroy on logout
useEffect(() => {
  if (!currentUser) {
    desktopHandsRef.current?.destroy();
    desktopHandsRef.current = null;
    return;
  }

  // Only connect in Tauri environment
  if (!("__TAURI_INTERNALS__" in window)) return;

  const apiBase = localStorage.getItem("chatdada_api_base") || "http://127.0.0.1:8000";
  const token = document.cookie
    .split("; ")
    .find((c) => c.startsWith("chat_dada_session="))
    ?.split("=")[1] || "";

  if (!token) return;

  const outputDir = localStorage.getItem("chatdada_output_dir") || defaultOutputDir();
  const client = new DesktopHandsClient(apiBase, token, outputDir);
  desktopHandsRef.current = client;
  client.connect();

  return () => {
    client.destroy();
    desktopHandsRef.current = null;
  };
}, [currentUser]);

function defaultOutputDir(): string {
  // Best-effort default — Tauri can resolve home dir at runtime
  return "~/Documents/ChatDaDa";
}
```

- [ ] **Step 2: Verify build**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada-front && npm run build`
Expected: Build succeeds

- [ ] **Step 3: Commit**

```bash
cd /Users/luozhongxu/Workspaces/chat-dada-front
git add src/App.tsx
git commit -m "feat(app): initialize DesktopHandsClient on auth in Tauri mode"
```

---

## Task 10: Integration Smoke Test

**Files:**
- No new files — verify end-to-end flow manually

- [ ] **Step 1: Run all backend tests**

Run: `.venv/bin/python -m pytest tests/test_desktop_manager.py tests/test_desktop_executor.py tests/test_gateway_desktop_routing.py tests/test_desktop_hands_ws.py -v`
Expected: All pass

- [ ] **Step 2: Run existing tests for regression**

Run: `.venv/bin/python -m pytest tests/test_platform_refactor.py tests/test_task_execution.py tests/test_coordinator_phase2_ppt.py -v`
Expected: All pass — no regressions from gateway changes

- [ ] **Step 3: Verify frontend builds**

Run: `cd /Users/luozhongxu/Workspaces/chat-dada-front && npm run build`
Expected: Build succeeds with no TypeScript errors

- [ ] **Step 4: Commit any remaining fixes**

If any tests failed, fix and commit. Otherwise, create a final integration commit:

```bash
cd /Users/luozhongxu/Workspaces/chat-dada
git add -A
git commit -m "feat: Desktop Hands — desktop tool execution via WebSocket

Implements the Desktop Hands channel (spec: 2026-04-13):
- DesktopHandsManager: connection registry + capabilities cache
- DesktopToolExecutor: ToolExecutor protocol over WebSocket
- ToolGateway: desktop routing with local fallback
- /ws/desktop-hands: authenticated WebSocket endpoint
- Frontend: DesktopHandsClient + OfficeCLI adapter + local file artifacts"
```
