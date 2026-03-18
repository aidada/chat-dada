"""
Three-tier research context management for deep_research agent.

Tiers:
  - Raw: full tool output (kept for most recent 2 steps)
  - Compact: key snippet + URLs (older entries)
  - Summary: global research summary

Zero external dependencies beyond langchain_core.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import ToolMessage

log = logging.getLogger("chatdada.context_manager")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RAW_CONTENT_THRESHOLD = 8000  # trigger compression when total raw chars exceed this
COMPACT_SNIPPET_LENGTH = 200  # chars kept per entry during compression
FINDING_ENTRY_VERSION = 1
CONTEXT_VERSION = 1

# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s\]\)\"'<>]+")
_DATA_PATTERN = re.compile(r'\d+\.?\d*\s*(%|m|km|cm|mm|ms|s|dB|Hz|MHz|GHz|accuracy|精度|误差)')


def _extract_urls(text: str) -> list[str]:
    """Extract unique URLs from *text*, preserving first-seen order."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.findall(text):
        url = match.rstrip(".,;:!?")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


# ---------------------------------------------------------------------------
# Message text helper (mirrors deep_research._message_text to avoid import)
# ---------------------------------------------------------------------------


def _message_text(message: ToolMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# FindingEntry
# ---------------------------------------------------------------------------


@dataclass
class FindingEntry:
    step: int
    tool_name: str
    query: str
    raw_content: str
    compact_content: str = ""
    source_urls: list[str] = field(default_factory=list)
    evidence_strength: str = ""
    key_claims: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "_version": FINDING_ENTRY_VERSION,
            "step": self.step,
            "tool_name": self.tool_name,
            "query": self.query,
            "raw_content": self.raw_content,
            "compact_content": self.compact_content,
            "source_urls": list(self.source_urls),
            "evidence_strength": self.evidence_strength,
            "key_claims": list(self.key_claims),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FindingEntry:
        version = data.get("_version", 0)
        if version != FINDING_ENTRY_VERSION:
            log.warning("FindingEntry version mismatch: expected %d, got %d", FINDING_ENTRY_VERSION, version)
        return cls(
            step=data.get("step", 0),
            tool_name=data.get("tool_name", ""),
            query=data.get("query", ""),
            raw_content=data.get("raw_content", ""),
            compact_content=data.get("compact_content", ""),
            source_urls=list(data.get("source_urls", [])),
            evidence_strength=data.get("evidence_strength", ""),
            key_claims=list(data.get("key_claims", [])),
        )


# ---------------------------------------------------------------------------
# ResearchContext
# ---------------------------------------------------------------------------


class ResearchContext:
    def __init__(self) -> None:
        self.entries: list[FindingEntry] = []
        self.summary: str = ""
        self.current_step: int = 0

    # -- mutators -----------------------------------------------------------

    def add_entry(self, entry: FindingEntry) -> None:
        self.entries.append(entry)
        if entry.step > self.current_step:
            self.current_step = entry.step

    def merge_tool_results(
        self, tool_messages: list[ToolMessage], step: int
    ) -> list[FindingEntry]:
        """Create FindingEntry objects from ToolMessages and add them."""
        created: list[FindingEntry] = []
        for msg in tool_messages:
            tool_name = str(getattr(msg, "name", "") or "tool")
            text = _message_text(msg)
            if not text:
                continue
            urls = _extract_urls(text)
            entry = FindingEntry(
                step=step,
                tool_name=tool_name,
                query="",
                raw_content=text,
                source_urls=urls,
            )
            self.add_entry(entry)
            created.append(entry)
        return created

    def _compact_entry(self, entry: FindingEntry, snippet_len: int = COMPACT_SNIPPET_LENGTH) -> None:
        """Compress entry using smart extraction of structured lines."""
        text = entry.raw_content

        key_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if (stripped.startswith(('#', '-', '•', '*', '>', '|'))
                or '**' in stripped
                or _DATA_PATTERN.search(stripped)):
                key_lines.append(stripped)

        if key_lines:
            snippet = '\n'.join(key_lines)[:snippet_len]
        else:
            snippet = text[:snippet_len]

        urls_str = ", ".join(entry.source_urls) if entry.source_urls else "(无来源)"
        entry.compact_content = f"{snippet}\n来源：{urls_str}"
        entry.raw_content = ""

    async def trigger_compression(self, step: int, token_budget: int = 0, query: str = "") -> None:
        """Compress old entries when total raw content exceeds threshold.

        Priority: weak/empty entries first, strong entries last (use LLM summary).
        Entries within 2 steps of current are never compressed.
        """
        budget = token_budget or RAW_CONTENT_THRESHOLD
        total_raw = sum(len(e.raw_content) for e in self.entries)
        if total_raw <= budget:
            return

        # Collect compressible candidates (age >= 2, has raw, not yet compacted)
        candidates = [
            e for e in self.entries
            if (step - e.step) >= 2 and e.raw_content and not e.compact_content
        ]
        # Sort: strong first (popped last since pop() takes from end)
        candidates.sort(key=lambda e: (
            0 if e.evidence_strength == "strong" else 1,
            e.step,
        ))

        while total_raw > budget and candidates:
            entry = candidates.pop()
            old_len = len(entry.raw_content)
            if entry.evidence_strength == "strong":
                try:
                    await self._llm_compact_entry(entry, query)
                except Exception:
                    self._compact_entry(entry)
            else:
                self._compact_entry(entry)
            total_raw -= old_len

        # Phase 2: aggressive compression if over token budget
        if token_budget > 0 and total_raw > token_budget:
            for entry in self.entries:
                if (step - entry.step) >= 1 and entry.raw_content and not entry.compact_content:
                    self._compact_entry(entry, snippet_len=100)

    async def _llm_compact_entry(self, entry: FindingEntry, query: str) -> None:
        """Compress a high-value entry using LLM summarization."""
        from models import get_llm, response_text
        from langchain_core.messages import SystemMessage, HumanMessage

        llm = get_llm("orchestrator")
        resp = await llm.ainvoke([
            SystemMessage(content="请把以下工具返回内容压缩为≤300字的结构化摘要，保留所有数据、URL和关键结论。"),
            HumanMessage(content=f"研究主题：{query}\n\n原文：\n{entry.raw_content[:3000]}"),
        ])
        entry.compact_content = response_text(resp)
        entry.raw_content = ""

    def update_summary(self, summary: str) -> None:
        self.summary = summary

    # -- output -------------------------------------------------------------

    def build_prompt_context(
        self,
        max_raw_steps: int = 2,
        max_compact_tokens: int = 4000,
        max_summary_tokens: int = 2000,
    ) -> str:
        """Build three-tier context string for the LLM prompt."""
        parts: list[str] = []

        # Tier 1: global summary
        summary_text = self.summary or "(暂无总结)"
        if len(summary_text) > max_summary_tokens:
            summary_text = summary_text[:max_summary_tokens] + "…"
        parts.append(f"## 研究总结\n{summary_text}")

        # Tier 2: compact old entries
        compact_parts: list[str] = []
        compact_len = 0
        for entry in self.entries:
            if not entry.compact_content:
                continue
            if compact_len + len(entry.compact_content) > max_compact_tokens:
                break
            compact_parts.append(
                f"[步骤{entry.step}] {entry.tool_name}: {entry.compact_content}"
            )
            compact_len += len(entry.compact_content)
        if compact_parts:
            parts.append("## 早期发现（压缩）\n" + "\n".join(compact_parts))

        # Tier 3: recent raw entries
        recent_entries = [
            e
            for e in self.entries
            if e.raw_content and (self.current_step - e.step) < max_raw_steps
        ]
        if recent_entries:
            raw_parts = [
                f"### [{e.tool_name}]\n{e.raw_content}" for e in recent_entries
            ]
            parts.append("## 最近发现（完整）\n" + "\n\n".join(raw_parts))

        return "\n\n".join(parts)

    def build_final_context(self, max_chars: int = 12000) -> str:
        """构建最终报告输入，按步骤排序，尽可能保留完整内容。"""
        parts: list[str] = []

        if self.summary:
            parts.append(f"## 研究总结\n{self.summary}")

        for entry in sorted(self.entries, key=lambda e: e.step):
            content = entry.raw_content or entry.compact_content
            if not content:
                continue
            urls = f"\n来源：{', '.join(entry.source_urls)}" if entry.source_urls else ""
            parts.append(f"### [步骤{entry.step}] {entry.tool_name}\n{content}{urls}")

        result = "\n\n".join(parts)
        if len(result) > max_chars:
            result = result[:max_chars - 1] + "…"
        return result

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "_version": CONTEXT_VERSION,
            "entries": [e.to_dict() for e in self.entries],
            "summary": self.summary,
            "current_step": self.current_step,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResearchContext:
        version = data.get("_version", 0)
        if version != CONTEXT_VERSION:
            log.warning("ResearchContext version mismatch: expected %d, got %d", CONTEXT_VERSION, version)
        ctx = cls()
        ctx.summary = data.get("summary", "")
        ctx.current_step = data.get("current_step", 0)
        for entry_data in data.get("entries", []):
            ctx.entries.append(FindingEntry.from_dict(entry_data))
        return ctx
