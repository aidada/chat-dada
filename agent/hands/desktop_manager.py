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
