"""AGENT_TYPE_REGISTRY — agent_type → graph factory 映射。

Root Graph 通过 agent_type 选择对应的 Sub Graph factory。
LLM 只能选择已注册的 agent_type，不能任意构造图。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent.sub_graphs.base import build_react_graph

AGENT_TYPE_REGISTRY: dict[str, Callable[[], Any]] = {
    "research":    build_react_graph,
    "patent":      build_react_graph,
    "office":      build_react_graph,
    "writer":      build_react_graph,
    "analyst":     build_react_graph,
}

AGENT_DEFAULT_DOMAINS: dict[str, str] = {
    "research":    "research",
    "patent":      "patent",
    "office":      "office",
    "writer":      "writing",
    "analyst":     "analysis",
}


def is_valid_agent_type(agent_type: str) -> bool:
    return agent_type in AGENT_TYPE_REGISTRY


def get_default_domain(agent_type: str) -> str | None:
    return AGENT_DEFAULT_DOMAINS.get(agent_type)


__all__ = [
    "AGENT_TYPE_REGISTRY",
    "AGENT_DEFAULT_DOMAINS",
    "get_default_domain",
    "is_valid_agent_type",
]
