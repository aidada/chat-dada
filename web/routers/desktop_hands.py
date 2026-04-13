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
