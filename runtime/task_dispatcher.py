from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable

from capabilities.general_chat import run as run_general_chat

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


@dataclass(frozen=True)
class RouteDecision:
    route_name: str
    reason: str
    confidence: float


def _matched_keywords(text: str, keywords: tuple[str, ...]) -> list[str]:
    return [keyword for keyword in keywords if keyword in text]


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


async def run_general_chat_task(
    task: str,
    on_step: Callable[[str], Awaitable[None]],
    user_id: str = "anonymous",
    conversation_context: str = "",
) -> str:
    await on_step("💬 正在回答...")

    async def on_chunk(content: str) -> None:
        if not content:
            return
        await on_step(json.dumps({"type": "token", "content": content}, ensure_ascii=False))
        await on_step(json.dumps({"type": "result_delta", "content": content}, ensure_ascii=False))

    result = await run_general_chat(
        {"query": task, "conversation_context": conversation_context},
        on_chunk=on_chunk,
    )
    if isinstance(result, dict):
        return str(result.get("result", result))
    return str(result)


async def dispatch_task(
    task_text: str,
    file_paths: list[str],
    mode: str = "auto",
    user_id: str = "anonymous",
) -> RouteDecision:
    route_name, reason, confidence = route_task_request(task_text, file_paths, mode)
    return RouteDecision(
        route_name=route_name,
        reason=reason,
        confidence=confidence,
    )
