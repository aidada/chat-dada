"""office Sub Graph — 使用通用 ReAct 引擎，office domain。"""
from agent.sub_graphs.base import build_react_graph as _build


def build_office_sub_graph():
    return _build()


__all__ = ["build_office_sub_graph"]
