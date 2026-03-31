from __future__ import annotations

from datetime import UTC, datetime


def render_markdown(report: str, *, title: str = "", timestamp: str = "") -> str:
    """给研究报告补上统一的 Markdown 头部。"""

    ts = timestamp or datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    header_parts: list[str] = []
    if title:
        header_parts.append(f"# {title}")
    header_parts.append(f"*Generated: {ts}*")
    header_parts.append("")  # 插入一个空行，避免标题和正文粘连
    header = "\n".join(header_parts)
    return f"{header}\n{report}" if report.strip() else header


def render_pptx(report: str, output_path: str, *, title: str = "Research Report") -> str:
    """通过共享 PPT 能力把研究报告渲染为 `.pptx`。"""
    from agent.capabilities.ppt_capability import markdown_to_deck, render_deck_to_pptx

    deck = markdown_to_deck(title, report)
    return render_deck_to_pptx(deck, output_path)
