"""
Research notes tools — allow the LLM agent to save and recall persistent notes.

Follows the ContextVar pattern from task_interaction.py.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any

from langchain_core.tools import tool

from research_memory import ResearchMemory

_current_memory: ContextVar[ResearchMemory | None] = ContextVar(
    "current_research_memory", default=None,
)
_current_step: ContextVar[int] = ContextVar(
    "current_research_step", default=0,
)


def set_research_context(memory: ResearchMemory | None, step: int) -> None:
    """Set the current ResearchMemory and step for tool access."""
    _current_memory.set(memory)
    _current_step.set(step)


@tool
async def save_research_note(topic: str, content: str, evidence_strength: str = "moderate") -> str:
    """保存重要研究笔记到持久存储。仅在发现关键结论、重要数据时使用。

    Args:
        topic: 笔记主题标签（如 "GNSS精度", "算法对比"）
        content: 笔记内容（关键结论或数据）
        evidence_strength: 证据强度（strong/moderate/weak）
    """
    try:
        memory = _current_memory.get()
        step = _current_step.get()
        if memory is None:
            return "研究记忆未初始化，笔记未保存。请继续研究。"
        memory.save_finding(step, "note", topic,
                            f"[evidence: {evidence_strength}]\n{content}", [])
        return f"笔记已保存：[{topic}] ({evidence_strength})"
    except Exception as e:
        return f"保存笔记失败：{e}。请继续研究。"


EVIDENCE_RANK = {"strong": 0, "moderate": 1, "weak": 2}


def _extract_evidence_strength(text: str) -> str:
    match = re.search(r"\[evidence:\s*(\w+)\]", text)
    return match.group(1) if match else "moderate"


@tool
async def recall_research_notes(topic: str = "") -> str:
    """检索已保存的研究笔记，可按主题关键词过滤。

    Args:
        topic: 可选的主题关键词过滤（留空返回所有）
    """
    try:
        memory = _current_memory.get()
        if memory is None:
            return "研究记忆未初始化，无法检索笔记。"
        findings = memory.list_findings()
        if not findings:
            return "暂无已保存的研究笔记。"

        # Collect and filter by topic keyword
        candidates: list[tuple[str, str]] = []  # (text, path_stem)
        for path in reversed(findings):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            if topic and topic.lower() not in text.lower():
                continue
            candidates.append((text, path.stem))

        # Sort by evidence strength (strong first)
        candidates.sort(key=lambda c: EVIDENCE_RANK.get(_extract_evidence_strength(c[0]), 2))

        # Return top 5
        results: list[str] = []
        for text, stem in candidates[:5]:
            snippet = text[:500] + ("..." if len(text) > 500 else "")
            results.append(f"[{stem}]\n{snippet}")

        if not results:
            return f"未找到包含 '{topic}' 的研究笔记。" if topic else "暂无已保存的研究笔记。"
        return "\n\n---\n\n".join(results)
    except Exception as e:
        return f"检索笔记失败：{e}"
