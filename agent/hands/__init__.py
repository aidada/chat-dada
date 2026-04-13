"""Hands 层 — 可替换执行端。

Brain 只通过 ToolGateway 调用工具，不关心具体执行环境。
所有 tool-call 业务事件由 ToolGateway 作为唯一权威点写入。

组件：
- ToolProtocol: 工具执行协议 (ToolCall, ToolResult, ToolContext, ToolExecutor)
- ToolGateway: 统一路由与事件记录
- LocalToolExecutor: 服务端工具封装
"""

from agent.hands.protocol import ToolCall, ToolContext, ToolExecutor, ToolResult
from agent.hands.gateway import ToolGateway
from agent.hands.local_executor import LocalToolExecutor

__all__ = [
    "LocalToolExecutor",
    "ToolCall",
    "ToolContext",
    "ToolExecutor",
    "ToolGateway",
    "ToolResult",
]
