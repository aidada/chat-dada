"""research Sub Graph — 使用通用 ReAct 引擎，research domain。"""
from agent.sub_graphs.base import build_react_graph as _build


def build_research_sub_graph():
    return _build()


__all__ = ["build_research_sub_graph"]
