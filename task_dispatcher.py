from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable

from agents.general_chat import run as run_general_chat
from orchestrator.runner import run_orchestrator

TaskExecutor = Callable[[str, Callable[[str], Awaitable[None]], str], Awaitable[str]]

AGENT_KEYWORDS = (
    "搜索",
    "查找",
    "检索",
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
    "research",
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


@dataclass(frozen=True)
class RouteDecision:
    route_name: str
    reason: str
    executor: TaskExecutor
    confidence: float


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

    if any(keyword in lowered for keyword in AGENT_KEYWORDS):
        return "orchestrator", "detected explicit tool or multi-step task keywords", 0.9

    if any(keyword in lowered for keyword in CHAT_KEYWORDS):
        return "general_chat", "detected direct chat / Q&A language", 0.85

    return "general_chat", "defaulted to direct chat because no orchestration signals were found", 0.65


async def run_general_chat_task(
    task: str,
    on_step: Callable[[str], Awaitable[None]],
    user_id: str = "anonymous",
) -> str:
    await on_step("💬 General Chat: 直接回答用户问题...")

    async def on_chunk(content: str) -> None:
        if not content:
            return
        await on_step(json.dumps({"type": "result_delta", "content": content}, ensure_ascii=False))

    result = await run_general_chat({"query": task}, on_chunk=on_chunk)
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
    executor = run_general_chat_task if route_name == "general_chat" else run_orchestrator
    return RouteDecision(
        route_name=route_name,
        reason=reason,
        executor=executor,
        confidence=confidence,
    )
