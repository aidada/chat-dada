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
from web.config import settings

log = logging.getLogger("chatdada.web.desktop_hands")


def _handshake_summary(ws: WebSocket) -> dict[str, Any]:
    headers = ws.headers
    cookie_names = sorted(ws.cookies.keys())
    return {
        "client": getattr(ws.client, "host", ""),
        "origin": headers.get("origin", ""),
        "host": headers.get("host", ""),
        "user_agent": headers.get("user-agent", ""),
        "cookie_names": cookie_names,
        "session_cookie_present": settings.session_cookie_name in ws.cookies,
    }


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
        handshake = _handshake_summary(ws)
        query_token_present = bool(token)
        token_source = "query" if query_token_present else "cookie" if handshake["session_cookie_present"] else "none"
        log.info(
            "Desktop WS handshake: client=%s origin=%s host=%s user_agent=%s token_source=%s session_cookie_present=%s query_token_present=%s",
            handshake["client"],
            handshake["origin"],
            handshake["host"],
            handshake["user_agent"],
            token_source,
            handshake["session_cookie_present"],
            query_token_present,
        )

        # 1. Authenticate
        if not token:
            token = ws.cookies.get(settings.session_cookie_name, "")
        user = await auth_fn(token)
        if user is None:
            log.warning(
                "Desktop WS rejected: missing/invalid auth token client=%s origin=%s host=%s token_source=%s cookie_names=%s",
                handshake["client"],
                handshake["origin"],
                handshake["host"],
                token_source,
                handshake["cookie_names"],
            )
            await ws.close(code=4001)
            return

        await ws.accept()
        user_id = user["id"]
        log.info(
            "Desktop WS accepted: user=%s client=%s origin=%s host=%s token_source=%s",
            user_id,
            handshake["client"],
            handshake["origin"],
            handshake["host"],
            token_source,
        )

        try:
            # 2. Wait for capabilities message
            caps_msg = await ws.receive_json()
            if caps_msg.get("type") != "capabilities":
                log.warning(
                    "Desktop WS expected capabilities first: user=%s received_type=%s",
                    user_id,
                    caps_msg.get("type", ""),
                )
                await ws.send_json({"type": "error", "payload": {"message": "Expected capabilities message"}})
                await ws.close(code=4002)
                return

            payload = caps_msg.get("payload", {})
            manager.register(user_id, ws, payload)

            # 3. Send ack
            tool_names = [t["name"] for t in payload.get("tools", []) if "name" in t]
            log.info(
                "Desktop capabilities received: user=%s platform=%s client_version=%s tool_count=%d tools=%s",
                user_id,
                payload.get("platform", ""),
                payload.get("client_version", ""),
                len(tool_names),
                tool_names,
            )
            if not tool_names:
                log.warning("Desktop capabilities empty: user=%s payload=%s", user_id, payload)
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

                elif msg_type == "ping":
                    await ws.send_json({
                        "type": "pong",
                        "id": f"pong_{msg.get('id', '')}",
                        "payload": {},
                    })

                else:
                    log.warning("Unknown desktop message type: %s", msg_type)

        except WebSocketDisconnect:
            log.info(
                "Desktop WS disconnected: user=%s client=%s origin=%s host=%s",
                user_id,
                handshake["client"],
                handshake["origin"],
                handshake["host"],
            )
        except Exception:
            log.exception(
                "Desktop WS error: user=%s client=%s origin=%s host=%s",
                user_id,
                handshake["client"],
                handshake["origin"],
                handshake["host"],
            )
        finally:
            manager.unregister(user_id, ws)

    return router
