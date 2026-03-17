"""
Layered user memory backed by markdown files.

Storage layout:
  data/memory/<user_id>/profile.md
  data/memory/<user_id>/timeline/YYYY/MM/YYYY-MM-DD.md
  data/memory/<user_id>/summaries/YYYY/YYYY-MM.md

Layers:
  - profile: durable user facts and preferences
  - summaries: rolling monthly digests
  - timeline: raw chronological interaction log
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from models import get_llm, response_text


PROFILE_SECTIONS = (
    "Identity",
    "Preferences",
    "Projects",
    "Working Style",
    "Constraints",
    "Open Loops",
)
DEFAULT_MEMORY_ROOT = Path(os.environ.get("LOCAL_AGENT_MEMORY_DIR", "data/memory"))
MAX_QUERY_SNIPPETS = 4
MAX_SUMMARY_LINES = 6
MAX_TIMELINE_FILES = 12
_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")


@dataclass(slots=True)
class MemorySnippet:
    timestamp: str
    source: str
    content: str
    score: float


@dataclass(slots=True)
class MemoryRecall:
    user_id: str
    profile_sections: dict[str, list[str]] = field(default_factory=dict)
    summary_lines: list[str] = field(default_factory=list)
    snippets: list[MemorySnippet] = field(default_factory=list)

    def has_content(self) -> bool:
        return bool(
            any(self.profile_sections.values())
            or self.summary_lines
            or self.snippets
        )

    def to_prompt(self) -> str:
        if not self.has_content():
            return ""

        lines = [
            "以下是和当前用户相关的记忆，仅在和本次任务相关时使用。",
            "如果与本轮明确请求冲突，以本轮请求为准。",
        ]

        non_empty_sections = [
            (name, items)
            for name, items in self.profile_sections.items()
            if items
        ]
        if non_empty_sections:
            lines.append("\n## 长期画像")
            for name, items in non_empty_sections:
                lines.append(f"### {name}")
                lines.extend(f"- {item}" for item in items[:5])

        if self.summary_lines:
            lines.append("\n## 近期摘要")
            lines.extend(f"- {line}" for line in self.summary_lines[:MAX_SUMMARY_LINES])

        if self.snippets:
            lines.append("\n## 相关历史片段")
            for snippet in self.snippets[:MAX_QUERY_SNIPPETS]:
                lines.append(
                    f"- [{snippet.timestamp}] {snippet.content}"
                )

        return "\n".join(lines).strip()


class MarkdownMemoryStore:
    def __init__(self, root: str | Path = DEFAULT_MEMORY_ROOT):
        self.root = Path(root)

    def recall(self, user_id: str, query: str) -> MemoryRecall:
        user = self._sanitize_user_id(user_id)
        user_dir = self.root / user
        recall = MemoryRecall(user_id=user)
        if not user_dir.exists():
            return recall

        recall.profile_sections = self._read_profile(user_dir / "profile.md")
        recall.summary_lines = self._read_summary_lines(user_dir)
        recall.snippets = self._recall_timeline(user_dir, query)
        return recall

    async def remember(
        self,
        user_id: str,
        task: str,
        result: str,
        *,
        intent: str,
    ) -> None:
        user = self._sanitize_user_id(user_id)
        now = datetime.now().astimezone()
        user_dir = self.root / user
        user_dir.mkdir(parents=True, exist_ok=True)

        self._append_timeline(user_dir, now, task, result, intent)
        self._append_monthly_summary(user_dir, now, task, result, intent)

        profile_updates = await self._extract_profile_updates(task, result)
        if profile_updates:
            current = self._read_profile(user_dir / "profile.md")
            merged = self._merge_profile_sections(current, profile_updates)
            self._write_profile(user_dir / "profile.md", merged, now)

    def _append_timeline(
        self,
        user_dir: Path,
        now: datetime,
        task: str,
        result: str,
        intent: str,
    ) -> None:
        timeline_dir = user_dir / "timeline" / now.strftime("%Y") / now.strftime("%m")
        timeline_dir.mkdir(parents=True, exist_ok=True)
        day_file = timeline_dir / f"{now.strftime('%Y-%m-%d')}.md"

        header = ""
        if not day_file.exists():
            header = (
                f"# Timeline {now.strftime('%Y-%m-%d')}\n\n"
                f"- user_id: {user_dir.name}\n"
                f"- generated_at: {now.isoformat()}\n\n"
            )

        entry = (
            f"## {now.isoformat()}\n"
            f"- intent: {intent}\n\n"
            f"### User\n"
            f"{task.strip() or '(empty)'}\n\n"
            f"### Assistant\n"
            f"{self._truncate(result, 4000)}\n\n"
        )
        with day_file.open("a", encoding="utf-8") as f:
            if header:
                f.write(header)
            f.write(entry)

    def _append_monthly_summary(
        self,
        user_dir: Path,
        now: datetime,
        task: str,
        result: str,
        intent: str,
    ) -> None:
        summary_dir = user_dir / "summaries" / now.strftime("%Y")
        summary_dir.mkdir(parents=True, exist_ok=True)
        month_file = summary_dir / f"{now.strftime('%Y-%m')}.md"

        if not month_file.exists():
            month_file.write_text(
                f"# Monthly Summary {now.strftime('%Y-%m')}\n\n## Highlights\n",
                encoding="utf-8",
            )

        bullet = (
            f"- {now.strftime('%Y-%m-%d %H:%M')} | {intent} | "
            f"用户: {self._one_line(task, 120)} | "
            f"结果: {self._one_line(result, 160)}\n"
        )
        with month_file.open("a", encoding="utf-8") as f:
            f.write(bullet)

    async def _extract_profile_updates(self, task: str, result: str) -> dict[str, list[str]]:
        llm = get_llm("orchestrator", temperature=0)
        prompt = f"""你在维护用户长期记忆。请只提取未来仍然有价值的稳定信息。

保留标准：
- 稳定身份信息
- 长期偏好
- 正在持续推进的项目
- 工作方式偏好
- 约束条件
- 未完成但可能需要后续跟进的事项

忽略：
- 只对本轮一次性有效的信息
- 纯临时上下文
- 助手自己的推断

输出 JSON，格式如下：
{{
  "Identity": [],
  "Preferences": [],
  "Projects": [],
  "Working Style": [],
  "Constraints": [],
  "Open Loops": []
}}

用户输入：
{self._truncate(task, 2000)}

助手输出：
{self._truncate(result, 2000)}
"""

        try:
            response = await llm.ainvoke(prompt)
            content = response_text(response).strip()
            if "```json" in content:
                content = content.split("```json", 1)[1].split("```", 1)[0]
            elif "```" in content:
                content = content.split("```", 1)[1].split("```", 1)[0]
            data = json.loads(content.strip())
        except Exception:
            return self._heuristic_profile_updates(task)

        updates: dict[str, list[str]] = {}
        for section in PROFILE_SECTIONS:
            raw_items = data.get(section, [])
            if isinstance(raw_items, str):
                raw_items = [raw_items]
            if not isinstance(raw_items, list):
                continue
            cleaned = [
                self._normalize_item(item)
                for item in raw_items
                if isinstance(item, str) and self._normalize_item(item)
            ]
            if cleaned:
                updates[section] = cleaned[:5]
        return updates

    def _heuristic_profile_updates(self, task: str) -> dict[str, list[str]]:
        signals = {
            "Identity": [r"我是(.{1,30})", r"我在(.{1,30})工作"],
            "Preferences": [r"我喜欢(.{1,30})", r"我希望(.{1,30})", r"以后(.{1,30})"],
            "Projects": [r"我的项目是(.{1,40})", r"我在做(.{1,40})"],
            "Constraints": [r"不要(.{1,30})", r"必须(.{1,30})"],
            "Open Loops": [r"帮我记住(.{1,40})", r"下次提醒我(.{1,40})"],
        }

        updates: dict[str, list[str]] = {}
        for section, patterns in signals.items():
            hits = []
            for pattern in patterns:
                hits.extend(match.strip() for match in re.findall(pattern, task))
            cleaned = [self._normalize_item(hit) for hit in hits if self._normalize_item(hit)]
            if cleaned:
                updates[section] = cleaned[:3]
        return updates

    def _read_profile(self, path: Path) -> dict[str, list[str]]:
        sections = {name: [] for name in PROFILE_SECTIONS}
        if not path.exists():
            return sections

        current: str | None = None
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                heading = line[3:].strip()
                current = heading if heading in sections else None
                continue
            if current and line.startswith("- "):
                item = self._normalize_item(line[2:])
                if item:
                    sections[current].append(item)
        return sections

    def _write_profile(
        self,
        path: Path,
        sections: dict[str, list[str]],
        now: datetime,
    ) -> None:
        lines = [
            "# User Profile Memory",
            "",
            f"- user_id: {path.parent.name}",
            f"- updated_at: {now.isoformat()}",
            "",
        ]
        for section in PROFILE_SECTIONS:
            lines.append(f"## {section}")
            items = sections.get(section, [])
            if items:
                lines.extend(f"- {item}" for item in items[:12])
            else:
                lines.append("-")
            lines.append("")
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

    def _merge_profile_sections(
        self,
        current: dict[str, list[str]],
        updates: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        merged: dict[str, list[str]] = {}
        for section in PROFILE_SECTIONS:
            merged[section] = self._dedupe_items(
                [*current.get(section, []), *updates.get(section, [])]
            )[:12]
        return merged

    def _read_summary_lines(self, user_dir: Path) -> list[str]:
        summaries_dir = user_dir / "summaries"
        if not summaries_dir.exists():
            return []

        month_files = sorted(summaries_dir.rglob("*.md"), reverse=True)[:2]
        lines: list[str] = []
        for file_path in month_files:
            for raw in file_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line.startswith("- "):
                    lines.append(line[2:])
        return lines[:MAX_SUMMARY_LINES]

    def _recall_timeline(self, user_dir: Path, query: str) -> list[MemorySnippet]:
        timeline_dir = user_dir / "timeline"
        if not timeline_dir.exists():
            return []

        query_tokens = self._tokenize(query)
        candidates: list[MemorySnippet] = []
        day_files = sorted(timeline_dir.rglob("*.md"), reverse=True)[:MAX_TIMELINE_FILES]

        for file_path in day_files:
            text = file_path.read_text(encoding="utf-8")
            blocks = re.split(r"(?m)^## ", text)
            for block in blocks[1:]:
                first_line, _, rest = block.partition("\n")
                timestamp = first_line.strip()
                content = self._summarize_block(rest)
                if not content:
                    continue

                tokens = self._tokenize(content)
                overlap = len(query_tokens & tokens)
                recency_bonus = max(0.0, 1.5 - 0.1 * len(candidates))
                score = overlap * 3 + recency_bonus

                if overlap == 0 and len(candidates) >= 2:
                    continue

                candidates.append(
                    MemorySnippet(
                        timestamp=timestamp,
                        source=str(file_path.relative_to(user_dir)),
                        content=content,
                        score=score,
                    )
                )

        ranked = sorted(candidates, key=lambda item: item.score, reverse=True)
        unique: list[MemorySnippet] = []
        seen = set()
        for item in ranked:
            key = item.content.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
            if len(unique) >= MAX_QUERY_SNIPPETS:
                break
        return unique

    def _summarize_block(self, block: str) -> str:
        user = ""
        assistant = ""
        user_match = re.search(r"### User\n(.*?)\n### Assistant", block, flags=re.S)
        if user_match:
            user = self._one_line(user_match.group(1), 120)
        assistant_match = re.search(r"### Assistant\n(.*)", block, flags=re.S)
        if assistant_match:
            assistant = self._one_line(assistant_match.group(1), 160)
        parts = []
        if user:
            parts.append(f"用户提到：{user}")
        if assistant:
            parts.append(f"助手回应：{assistant}")
        return " | ".join(parts)

    def _sanitize_user_id(self, user_id: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (user_id or "anonymous").strip())
        return cleaned[:80] or "anonymous"

    def _dedupe_items(self, items: Iterable[str]) -> list[str]:
        deduped: list[str] = []
        seen = set()
        for item in items:
            normalized = item.casefold()
            if not item or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(item)
        return deduped

    def _normalize_item(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.replace("- ", "").strip(" -"))

    def _tokenize(self, text: str) -> set[str]:
        tokens: set[str] = set()
        for match in _WORD_RE.findall(text.lower()):
            if re.fullmatch(r"[\u4e00-\u9fff]{2,}", match):
                tokens.add(match)
                tokens.update(match[i:i + 2] for i in range(len(match) - 1))
            else:
                tokens.add(match)
        return tokens

    def _truncate(self, text: str, limit: int) -> str:
        text = str(text).strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def _one_line(self, text: str, limit: int) -> str:
        compact = re.sub(r"\s+", " ", str(text)).strip()
        return self._truncate(compact, limit)


_MEMORY_STORE = MarkdownMemoryStore()


def get_memory_store() -> MarkdownMemoryStore:
    return _MEMORY_STORE
