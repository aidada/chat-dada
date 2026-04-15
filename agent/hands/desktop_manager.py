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

DesktopPathAliases = dict[str, str]


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
    tool_descriptors: dict[str, dict[str, Any]] = field(default_factory=dict)
    path_aliases: DesktopPathAliases = field(default_factory=dict)

    def has_tool(self, name: str) -> bool:
        return name in self.tool_names

    def get_tool_descriptor(self, name: str) -> dict[str, Any] | None:
        return self.tool_descriptors.get(name)

    def list_tool_descriptors(self) -> list[dict[str, Any]]:
        return list(self.tool_descriptors.values())


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
        previous = self._connections.get(user_id)
        tools = capabilities.get("tools", [])
        raw_paths = capabilities.get("paths", {})
        tool_descriptors = {
            str(tool["name"]): dict(tool)
            for tool in tools
            if isinstance(tool, dict) and "name" in tool
        }
        path_aliases = {
            str(name): str(path)
            for name, path in raw_paths.items()
            if isinstance(raw_paths, dict)
            and isinstance(name, str)
            and isinstance(path, str)
            and path.strip()
        }
        tool_names = set(tool_descriptors)
        conn = DesktopConnection(
            ws=ws,
            capabilities=capabilities,
            tool_names=tool_names,
            tool_descriptors=tool_descriptors,
            path_aliases=path_aliases,
        )
        self._connections[user_id] = conn
        if previous is not None and previous.ws is not ws:
            log.info("Desktop connection replaced: user=%s", user_id)
        log.info(
            "Desktop connected: user=%s tools=%s",
            user_id,
            sorted(tool_names),
        )
        return conn

    def unregister(self, user_id: str, ws: Any | None = None) -> None:
        current = self._connections.get(user_id)
        if current is None:
            return
        if ws is not None and current.ws is not ws:
            log.info("Desktop disconnect ignored for stale connection: user=%s", user_id)
            return
        removed = self._connections.pop(user_id, None)
        if removed:
            log.info("Desktop disconnected: user=%s", user_id)

    def get_connection(self, user_id: str) -> DesktopConnection | None:
        return self._connections.get(user_id)

    def is_connected(self, user_id: str) -> bool:
        return user_id in self._connections

    def get_tool_descriptor(self, user_id: str, tool_name: str) -> dict[str, Any] | None:
        conn = self.get_connection(user_id)
        if conn is None:
            return None
        return conn.get_tool_descriptor(tool_name)

    def list_tool_descriptors(self, user_id: str) -> list[dict[str, Any]]:
        conn = self.get_connection(user_id)
        if conn is None:
            return []
        return conn.list_tool_descriptors()

    def get_path_aliases(self, user_id: str) -> DesktopPathAliases:
        conn = self.get_connection(user_id)
        if conn is None:
            return {}
        return dict(conn.path_aliases)
