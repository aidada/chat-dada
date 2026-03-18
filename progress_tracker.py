"""
Runtime progress tracking for deep_research agent.

Maintains structured working state: what searches were done, what was found,
what gaps remain. Generates an attention block for the LLM prompt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_KEY_FINDINGS = 10
MAX_COMPLETED_SEARCHES = 30
MAX_FAILED_SEARCHES = 10
MAX_REMAINING_GAPS = 10
ATTENTION_BLOCK_MAX_CHARS = 1500
TRACKER_VERSION = 1

# ---------------------------------------------------------------------------
# ProgressTracker
# ---------------------------------------------------------------------------


@dataclass
class ProgressTracker:
    original_query: str = ""
    clarified_goal: str = ""
    subtasks_status: list[dict] = field(default_factory=list)
    completed_searches: list[str] = field(default_factory=list)
    failed_searches: list[str] = field(default_factory=list)
    key_findings_so_far: list[str] = field(default_factory=list)
    remaining_gaps: list[str] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    def record_token_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Record token usage from an LLM call."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens

    def record_search(self, query: str, success: bool) -> None:
        """Record a search query. Deduplicates, caps each list."""
        if success:
            if query not in self.completed_searches:
                self.completed_searches.append(query)
                if len(self.completed_searches) > MAX_COMPLETED_SEARCHES:
                    self.completed_searches = self.completed_searches[-MAX_COMPLETED_SEARCHES:]
        else:
            if query not in self.failed_searches:
                self.failed_searches.append(query)
                if len(self.failed_searches) > MAX_FAILED_SEARCHES:
                    self.failed_searches = self.failed_searches[-MAX_FAILED_SEARCHES:]

    def record_finding(self, finding: str) -> None:
        """Append a one-sentence finding summary. FIFO cap at MAX_KEY_FINDINGS."""
        self.key_findings_so_far.append(finding)
        if len(self.key_findings_so_far) > MAX_KEY_FINDINGS:
            self.key_findings_so_far = self.key_findings_so_far[-MAX_KEY_FINDINGS:]

    def record_gap(self, gap: str) -> None:
        """Append a gap description. Cap at MAX_REMAINING_GAPS."""
        self.remaining_gaps.append(gap)
        if len(self.remaining_gaps) > MAX_REMAINING_GAPS:
            self.remaining_gaps = self.remaining_gaps[-MAX_REMAINING_GAPS:]

    def resolve_gap(self, keyword: str) -> None:
        """移除包含 keyword 的缺口。"""
        self.remaining_gaps = [g for g in self.remaining_gaps if keyword.lower() not in g.lower()]

    def update_subtask(self, subtask_id: str, status: str) -> None:
        """Update or add subtask status (for P1-1 hierarchical planning)."""
        for st in self.subtasks_status:
            if st.get("id") == subtask_id:
                st["status"] = status
                return
        self.subtasks_status.append({"id": subtask_id, "status": status})

    def build_attention_block(self) -> str:
        """Generate a structured progress block (<1500 chars) for LLM prompt."""
        lines: list[str] = ["---", "研究进度："]
        lines.append(f"目标：{self.original_query or '(未设定)'}")

        # Completed searches — show last 5
        n_completed = len(self.completed_searches)
        recent_completed = self.completed_searches[-5:]
        lines.append(f"已完成搜索 ({n_completed}): {' | '.join(recent_completed) if recent_completed else '(无)'}")

        # Key findings — show last 5
        n_findings = len(self.key_findings_so_far)
        recent_findings = self.key_findings_so_far[-5:]
        if recent_findings:
            lines.append(f"关键发现 ({n_findings}/{MAX_KEY_FINDINGS}):")
            for f in recent_findings:
                lines.append(f"- {f}")
        else:
            lines.append(f"关键发现 (0/{MAX_KEY_FINDINGS}): (暂无)")

        # Remaining gaps — show last 3
        recent_gaps = self.remaining_gaps[-3:]
        lines.append(f"未覆盖缺口: {' | '.join(recent_gaps) if recent_gaps else '(暂无)'}")

        # Failed searches — show last 3
        recent_failed = self.failed_searches[-3:]
        lines.append(f"已失败搜索（不要重复）: {' | '.join(recent_failed) if recent_failed else '(无)'}")

        # Token usage
        if self.total_input_tokens > 0:
            lines.append(f"累计 token: 输入 {self.total_input_tokens} / 输出 {self.total_output_tokens}")

        lines.append("---")
        block = "\n".join(lines)

        # Truncate if exceeds max chars
        if len(block) > ATTENTION_BLOCK_MAX_CHARS:
            block = block[:ATTENTION_BLOCK_MAX_CHARS - 3] + "..."
        return block

    def to_dict(self) -> dict[str, Any]:
        return {
            "_version": TRACKER_VERSION,
            "original_query": self.original_query,
            "clarified_goal": self.clarified_goal,
            "subtasks_status": list(self.subtasks_status),
            "completed_searches": list(self.completed_searches),
            "failed_searches": list(self.failed_searches),
            "key_findings_so_far": list(self.key_findings_so_far),
            "remaining_gaps": list(self.remaining_gaps),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgressTracker:
        version = data.get("_version", 0)
        if version != TRACKER_VERSION:
            import logging
            logging.getLogger("chatdada.progress_tracker").warning(
                "ProgressTracker version mismatch: expected %d, got %d", TRACKER_VERSION, version
            )
        return cls(
            original_query=data.get("original_query", ""),
            clarified_goal=data.get("clarified_goal", ""),
            subtasks_status=list(data.get("subtasks_status", [])),
            completed_searches=list(data.get("completed_searches", [])),
            failed_searches=list(data.get("failed_searches", [])),
            key_findings_so_far=list(data.get("key_findings_so_far", [])),
            remaining_gaps=list(data.get("remaining_gaps", [])),
            total_input_tokens=data.get("total_input_tokens", 0),
            total_output_tokens=data.get("total_output_tokens", 0),
        )


# ---------------------------------------------------------------------------
# Gap extraction from structured summaries
# ---------------------------------------------------------------------------

_GAP_KEYWORDS = re.compile(r"缺口|缺少|未覆盖|尚未|不足|need|missing|gap", re.IGNORECASE)
_NEXT_STEP_KEYWORDS = re.compile(r"下一步|next step", re.IGNORECASE)


def extract_gaps_from_summary(summary: str) -> list[str]:
    """从结构化摘要中规则提取缺口描述。

    匹配模式：
    - "缺口" / "缺少" / "未覆盖" / "尚未" / "need" / "missing" 后的句子
    - "下一步" 段落中的建议
    每条缺口≤80字符，最多返回 5 条。
    """
    if not summary or not summary.strip():
        return []

    gaps: list[str] = []
    in_next_step_section = False

    for line in summary.splitlines():
        stripped = line.strip().lstrip("-•*0123456789.）) ")
        if not stripped:
            continue

        if _NEXT_STEP_KEYWORDS.search(line):
            in_next_step_section = True
            if len(stripped) > 10 and not stripped.endswith("：") and not stripped.endswith(":"):
                gaps.append(stripped[:80])
            continue

        if line.startswith("#") or (line.startswith("**") and line.endswith("**")):
            in_next_step_section = False
            continue

        if _GAP_KEYWORDS.search(line):
            gaps.append(stripped[:80])
        elif in_next_step_section and stripped:
            gaps.append(stripped[:80])

        if len(gaps) >= 5:
            break

    return gaps[:5]


# ---------------------------------------------------------------------------
# Rule-based progress extraction from tool results
# ---------------------------------------------------------------------------


def _message_text(message: ToolMessage) -> str:
    """Extract text from a ToolMessage."""
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


def extract_progress_from_tool_results(
    tool_messages: list[ToolMessage],
    prev_ai_message: AIMessage | None,
) -> tuple[list[str], list[str], list[str]]:
    """Rule-based extraction of progress from tool results.

    Returns (completed_queries, key_findings, failed_queries).
    - Extracts query param from prev_ai_message.tool_calls
    - Non-empty tool result → completed; empty/error → failed
    - First sentence (≤120 chars) of each tool result → key_finding (max 1 per tool)
    """
    completed_queries: list[str] = []
    key_findings: list[str] = []
    failed_queries: list[str] = []

    # Build mapping from tool_call_id → query
    call_queries: dict[str, str] = {}
    if prev_ai_message and hasattr(prev_ai_message, "tool_calls"):
        for tc in prev_ai_message.tool_calls:
            call_id = tc.get("id", "")
            args = tc.get("args", {})
            query = args.get("query", args.get("task_description", args.get("topic", "")))
            if call_id:
                call_queries[call_id] = str(query or "")

    for msg in tool_messages:
        text = _message_text(msg)
        call_id = getattr(msg, "tool_call_id", "")
        query = call_queries.get(call_id, "")

        if text and text.strip() and not text.startswith("(") and "not configured" not in text.lower():
            if query:
                completed_queries.append(query)
            # Extract first sentence as key finding (≤120 chars)
            first_line = text.strip().split("\n")[0]
            finding = first_line[:120]
            key_findings.append(finding)
        else:
            if query:
                failed_queries.append(query)

    return completed_queries, key_findings, failed_queries
