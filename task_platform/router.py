from __future__ import annotations

import logging

from runtime.task_dispatcher import RouteDecision

from task_platform.state import RouteDecisionPayload

_log = logging.getLogger("chatdada.router")


RESEARCH_KEYWORDS = (
    "研究",
    "调研",
    "论文",
    "文献",
    "综述",
    "evidence",
    "citation",
    "literature",
    "research",
    "paper",
    "papers",
    "survey",
)

PATENT_KEYWORDS = (
    "专利",
    "权利要求",
    "技术交底",
    "现有技术",
    "说明书",
    "claim tree",
    "prior art",
    "patent",
)

ZERO_REPORT_KEYWORDS = (
    "归零",
    "归零报告",
    "复盘报告",
    "根因分析",
    "整改矩阵",
    "postmortem",
    "zero report",
)

PPT_KEYWORDS = (
    "ppt",
    "幻灯片",
    "演示文稿",
    "slide",
    "slides",
    "powerpoint",
    "presentation",
    "deck",
)


def is_research_task(task_text: str, file_paths: list[str]) -> bool:
    lowered = (task_text or "").lower()
    if any(keyword in lowered for keyword in RESEARCH_KEYWORDS):
        return True
    return False


def needs_clarification(task_text: str, decision: RouteDecision) -> bool:
    text = (task_text or "").strip()
    if not text:
        return False
    if decision.confidence >= 0.72:
        return False
    if len(text) < 18:
        return True
    return False


def build_route_payload(
    *,
    task_text: str,
    file_paths: list[str],
    decision: RouteDecision,
) -> RouteDecisionPayload:
    lowered = (task_text or "").lower()

    if decision.route_name == "general_chat":
        execution_path: RouteDecisionPayload["execution_path"] = "general_chat"
    elif any(keyword in lowered for keyword in PATENT_KEYWORDS):
        execution_path = "patent"
    elif any(keyword in lowered for keyword in ZERO_REPORT_KEYWORDS):
        execution_path = "zero_report"
    elif needs_clarification(task_text, decision):
        execution_path = "needs_clarification"
    elif file_paths:
        execution_path = "research"
    elif is_research_task(task_text, file_paths):
        execution_path = "research"
    else:
        execution_path = "general_chat"

    # PPT keywords override — highest priority among content-based routes
    if any(keyword in lowered for keyword in PPT_KEYWORDS):
        execution_path = "ppt"

    from task_platform.domain_registry import registry as domain_registry

    if not domain_registry.is_registered(execution_path) and execution_path not in (
        "general_chat",
        "needs_clarification",
    ):
        _log.warning("Routed to unregistered domain %r — execution may fail", execution_path)

    return {
        "route_name": execution_path,
        "reason": decision.reason,
        "confidence": decision.confidence,
        "execution_path": execution_path,
    }
