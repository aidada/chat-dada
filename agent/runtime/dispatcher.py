from __future__ import annotations

import logging
from dataclasses import dataclass

from agent.platform.state import RouteDecisionPayload

_log = logging.getLogger("chatdada.router")

AGENT_KEYWORDS = (
    "搜索",
    "查找",
    "检索",
    "研究",
    "调研",
    "深度研究",
    "论文",
    "文献",
    "综述",
    "参考文献",
    "浏览",
    "打开",
    "访问",
    "读取",
    "分析文件",
    "分析附件",
    "整理成报告",
    "生成报告",
    "保存到",
    "导出",
    "ppt",
    "幻灯片",
    "画",
    "绘制",
    "生成图",
    "生图",
    "头像",
    "插画",
    "图片",
    "壁纸",
    "logo",
    "海报",
    "draw",
    "generate image",
    "image",
    "illustration",
    "research",
    "paper",
    "papers",
    "literature",
    "survey",
    "search",
    "browse",
    "open",
    "read file",
    "analyze file",
    "report",
    "export",
)

CHAT_KEYWORDS = (
    "hi",
    "hello",
    "hey",
    "你好",
    "您好",
    "早上好",
    "晚上好",
    "请问",
    "解释",
    "什么是",
    "为什么",
    "怎么",
    "如何",
    "能不能",
    "翻译",
    "改写",
    "润色",
    "总结一下",
)

CAPABILITY_QUERY_PREFIXES = (
    "你能",
    "你可以",
    "你会",
    "能不能",
    "可不可以",
    "可以不可以",
)

MULTI_STEP_HINTS = (
    "同时",
    "并且",
    "以及",
    "还要",
    "还需要",
    "用于",
    "先",
    "再",
)

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


@dataclass(frozen=True)
class RouteDecision:
    route_name: str
    reason: str
    confidence: float


def _matched_keywords(text: str, keywords: tuple[str, ...]) -> list[str]:
    return [keyword for keyword in keywords if keyword in text]


def _is_capability_query(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    if len(lowered) > 40:
        return False
    if not any(lowered.startswith(prefix) for prefix in CAPABILITY_QUERY_PREFIXES):
        return False
    if not any(token in lowered for token in ("帮我", "写论文", "论文", "研究", "文献", "综述")):
        return False
    return lowered.endswith(("吗", "么", "?", "？"))


def route_task_request(task_text: str, file_paths: list[str], mode: str = "auto") -> tuple[str, str, float]:
    normalized_mode = (mode or "auto").strip().lower()
    text = (task_text or "").strip()
    lowered = text.lower()

    if normalized_mode == "chat":
        return "general_chat", "forced by mode=chat", 1.0
    if normalized_mode == "agent":
        return "orchestrator", "forced by mode=agent", 1.0

    if file_paths:
        return "orchestrator", "attachments require tool-capable orchestration", 0.98

    if _is_capability_query(text):
        return "general_chat", "detected capability inquiry rather than an execution request", 0.92

    agent_hits = _matched_keywords(lowered, AGENT_KEYWORDS)
    if agent_hits:
        hit_preview = ", ".join(agent_hits[:3])
        return "orchestrator", f"detected research/tool task keywords: {hit_preview}", 0.9

    multi_step_hits = _matched_keywords(lowered, MULTI_STEP_HINTS)
    if len(text) >= 40 and len(multi_step_hits) >= 2:
        hit_preview = ", ".join(multi_step_hits[:3])
        return "orchestrator", f"detected multi-step planning hints: {hit_preview}", 0.82

    chat_hits = _matched_keywords(lowered, CHAT_KEYWORDS)
    if chat_hits:
        hit_preview = ", ".join(chat_hits[:3])
        return "general_chat", f"detected direct chat / Q&A language: {hit_preview}", 0.85

    return "general_chat", "defaulted to direct chat because no orchestration signals were found", 0.65


async def dispatch_task(
    task_text: str,
    file_paths: list[str],
    mode: str = "auto",
    user_id: str = "anonymous",
) -> RouteDecision:
    route_name, reason, confidence = route_task_request(task_text, file_paths, mode)
    return RouteDecision(route_name=route_name, reason=reason, confidence=confidence)


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

    if any(keyword in lowered for keyword in PPT_KEYWORDS):
        execution_path = "ppt"

    # Detect composite (cross-domain) tasks: multiple domain hits + multi-step hints
    domain_hits = sum(
        [
            bool(any(k in lowered for k in RESEARCH_KEYWORDS)),
            bool(any(k in lowered for k in PATENT_KEYWORDS)),
            bool(any(k in lowered for k in ZERO_REPORT_KEYWORDS)),
            bool(any(k in lowered for k in PPT_KEYWORDS)),
        ]
    )
    multi_step_hits = _matched_keywords(lowered, MULTI_STEP_HINTS)
    if domain_hits >= 2 or (domain_hits >= 1 and len(multi_step_hits) >= 2):
        execution_path = "composite"

    from agent.coordinator.skills import skill_registry

    # Convert execution_path to skill name format for checking
    skill_name = f"do_{execution_path}" if not execution_path.startswith("do_") else execution_path
    if not skill_registry.is_registered(skill_name) and execution_path not in (
        "general_chat",
        "needs_clarification",
        "composite",
    ):
        _log.warning("Routed to unregistered domain %r — execution may fail", execution_path)

    return {
        "route_name": execution_path,
        "reason": decision.reason,
        "confidence": decision.confidence,
        "execution_path": execution_path,
    }


__all__ = [
    "AGENT_KEYWORDS",
    "CHAT_KEYWORDS",
    "MULTI_STEP_HINTS",
    "RouteDecision",
    "build_route_payload",
    "dispatch_task",
    "is_research_task",
    "needs_clarification",
    "route_task_request",
]
