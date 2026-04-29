"""build_root_graph — 构建新的 Root Graph（depth=0，单一编排控制面）。"""
from __future__ import annotations

from typing import Any

from langgraph.constants import END, START
from langgraph.graph import StateGraph

from agent.root.nodes.build_agents import build_agents
from agent.root.nodes.check_agents import check_agents
from agent.root.nodes.direct_answer import direct_answer
from agent.root.nodes.execute_agents import execute_agents
from agent.root.nodes.finalize import finalize
from agent.root.nodes.normalize import normalize_input
from agent.root.nodes.synthesize import synthesize
from agent.root.nodes.understand_goal import understand_goal


def route_after_understand(state: dict[str, Any]) -> str:
    mode = str(state.get("execution_mode", "direct"))
    if mode == "direct":
        return "direct_answer"
    return "build_agents"


def route_after_check(state: dict[str, Any]) -> str:
    if state.get("_continue"):
        return "execute_agents"
    return "synthesize"


def build_root_graph(*, checkpointer: Any = None):
    graph = StateGraph(dict)

    graph.add_node("normalize_input", normalize_input)
    graph.add_node("understand_goal", understand_goal)
    graph.add_node("direct_answer", direct_answer)
    graph.add_node("build_agents", build_agents)
    graph.add_node("execute_agents", execute_agents)
    graph.add_node("check_agents", check_agents)
    graph.add_node("synthesize", synthesize)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "normalize_input")
    graph.add_edge("normalize_input", "understand_goal")
    graph.add_conditional_edges(
        "understand_goal",
        route_after_understand,
        {"direct_answer": "direct_answer", "build_agents": "build_agents"},
    )
    graph.add_edge("direct_answer", "finalize")
    graph.add_edge("build_agents", "execute_agents")
    graph.add_edge("execute_agents", "check_agents")
    graph.add_conditional_edges(
        "check_agents",
        route_after_check,
        {"execute_agents": "execute_agents", "synthesize": "synthesize"},
    )
    graph.add_edge("synthesize", "finalize")
    graph.add_edge("finalize", END)

    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()

    return graph.compile(checkpointer=checkpointer, name="chat_dada_root_graph")


__all__ = ["build_root_graph"]
