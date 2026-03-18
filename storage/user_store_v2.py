"""Structured user memory store with semantic facts and project lifecycle."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from core.models import get_llm, response_text
from storage.user_models import UserFact, Project, UserMemoryData

OFFLINE_THRESHOLD_DAYS = 14
CONFIDENCE_DECAY = 0.7
MAX_RECALL_FACTS = 10
PENDING_MERGE_THRESHOLD = 8
HOT_RETENTION_DAYS = 7


@dataclass
class MemoryRecallV2:
    user_id: str = ""
    facts: list[UserFact] = field(default_factory=list)
    active_projects: list[Project] = field(default_factory=list)
    stale_projects: list[Project] = field(default_factory=list)
    recent_timeline: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def has_content(self) -> bool:
        return bool(self.facts or self.active_projects or self.stale_projects or self.recent_timeline)

    def to_prompt(self) -> str:
        if not self.has_content():
            return ""

        parts = [
            "以下是和当前用户相关的记忆，仅在和本次任务相关时使用。",
            "如果与本轮明确请求冲突，以本轮请求为准。",
        ]

        if self.notes:
            parts.append("\n## 注意\n" + "\n".join(f"- {n}" for n in self.notes))

        if self.facts:
            top = sorted(self.facts, key=lambda f: f.confidence, reverse=True)[:MAX_RECALL_FACTS]
            parts.append("\n## 用户画像")
            for f in top:
                parts.append(f"- {f.content}")

        if self.active_projects:
            parts.append("\n## 当前项目")
            for p in self.active_projects:
                desc = f": {p.description}" if p.description else ""
                parts.append(f"- {p.name}{desc}")

        if self.stale_projects:
            parts.append("\n## 可能已完成的项目")
            for p in self.stale_projects:
                date = p.updated_at[:10] if p.updated_at else "?"
                parts.append(f"- {p.name} (最后活跃: {date})")

        if self.recent_timeline:
            parts.append("\n## 最近交互")
            for line in self.recent_timeline[:4]:
                parts.append(f"- {line}")

        return "\n".join(parts).strip()


class MemoryStoreV2:
    def __init__(self, root: str | Path = "data/memory"):
        self.root = Path(root)

    def recall(self, user_id: str, query: str) -> MemoryRecallV2:
        user = self._sanitize(user_id)
        user_dir = self.root / user
        recall = MemoryRecallV2(user_id=user)

        if not user_dir.exists():
            return recall

        mem = UserMemoryData.load(user_dir)

        # --- offline return detection ---
        last_seen = mem.meta.get("last_seen", "")
        gap_days = 0
        if last_seen:
            try:
                gap_days = (datetime.now(timezone.utc) - datetime.fromisoformat(last_seen)).days
            except (ValueError, TypeError):
                pass

        if gap_days > OFFLINE_THRESHOLD_DAYS:
            for f in mem.facts:
                f.confidence *= CONFIDENCE_DECAY
            for p in mem.projects:
                if p.status == "active":
                    p.status = "stale"
            mem.save(user_dir)

            recall.notes.append(
                f"⚠ 用户已 {gap_days} 天未互动。"
                f"项目状态可能已过时，建议在合适时机确认用户当前工作重点。"
            )

        # --- auto-stale detection for active projects ---
        for p in mem.projects:
            if p.status == "active" and p.is_stale():
                p.status = "stale"

        # --- build recall ---
        active_facts = [f for f in mem.facts if f.is_active()] + [f for f in mem.pending_facts if f.is_active()]
        active_facts.sort(key=lambda f: f.confidence, reverse=True)
        recall.facts = active_facts

        recall.active_projects = [p for p in mem.projects if p.status == "active"]
        recall.stale_projects = [p for p in mem.projects if p.status == "stale"]

        return recall

    def _sanitize(self, user_id: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (user_id or "anonymous").strip())
        return cleaned[:80] or "anonymous"

    async def remember(
        self, user_id: str, task: str, result: str, *, intent: str
    ) -> None:
        user = self._sanitize(user_id)
        user_dir = self.root / user
        user_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)

        mem = UserMemoryData.load(user_dir)
        mem.meta["last_seen"] = now.isoformat()
        mem.meta["interaction_count"] = mem.meta.get("interaction_count", 0) + 1

        # 1. Append to hot timeline
        self._append_hot_timeline(user_dir, now, task, result, intent)

        # 2. Extract new facts via LLM
        new_items = await self._extract_facts(task, result)
        for item in new_items:
            if item.get("category") == "project":
                self._upsert_project(mem, item.get("content", ""), now)
            else:
                mem.pending_facts.append(UserFact(
                    category=item.get("category", "identity"),
                    content=item.get("content", ""),
                ))

        # 3. Update active project timestamps if mentioned
        task_lower = task.lower()
        for p in mem.projects:
            if p.status in ("active", "stale") and p.name.lower()[:10] in task_lower:
                p.updated_at = now.isoformat()
                if p.status == "stale":
                    p.status = "active"

        mem.save(user_dir)

    def _upsert_project(self, mem: UserMemoryData, name: str, now: datetime) -> None:
        """Create project if not exists, or update if name is similar."""
        for p in mem.projects:
            if self._similar_name(p.name, name):
                p.updated_at = now.isoformat()
                if p.status == "stale":
                    p.status = "active"
                return
        mem.projects.append(Project(name=name, description=name))

    def _similar_name(self, a: str, b: str) -> bool:
        """Basic similarity: shared tokens > 50% of shorter string's tokens."""
        tokens_a = set(a.lower().split())
        tokens_b = set(b.lower().split())
        if not tokens_a or not tokens_b:
            return a.lower().strip() == b.lower().strip()
        overlap = len(tokens_a & tokens_b)
        return overlap >= len(min(tokens_a, tokens_b, key=len)) * 0.5

    async def _extract_facts(self, task: str, result: str) -> list[dict]:
        llm = get_llm("orchestrator", temperature=0)
        prompt = f"""从对话中提取用户的稳定信息。返回 JSON 数组。

每个元素格式：{{"category": "identity|preference|constraint|working_style|project", "content": "..."}}

只提取：稳定身份、长期偏好、约束条件、工作方式、项目/课题名称。
忽略：临时上下文、助手推断、一次性信息。

用户：{task[:1500]}
助手：{result[:1500]}"""

        try:
            response = await llm.ainvoke(prompt)
            content = response_text(response).strip()
            if "```json" in content:
                content = content.split("```json", 1)[1].split("```", 1)[0]
            elif "```" in content:
                content = content.split("```", 1)[1].split("```", 1)[0]
            items = json.loads(content.strip())
            if not isinstance(items, list):
                return []
            return [item for item in items if isinstance(item, dict) and item.get("content")]
        except Exception:
            return []

    def _append_hot_timeline(
        self, user_dir: Path, now: datetime, task: str, result: str, intent: str
    ) -> None:
        hot_dir = user_dir / "timeline" / "hot"
        hot_dir.mkdir(parents=True, exist_ok=True)
        day_file = hot_dir / f"{now.strftime('%Y-%m-%d')}.md"

        header = ""
        if not day_file.exists():
            header = f"# Timeline {now.strftime('%Y-%m-%d')}\n\n"

        entry = (
            f"## {now.isoformat()}\n"
            f"- intent: {intent}\n\n"
            f"### User\n{task[:300]}\n\n"
            f"### Assistant\n{result[:2000]}\n\n"
        )
        with day_file.open("a", encoding="utf-8") as f:
            if header:
                f.write(header)
            f.write(entry)

    async def recall_with_merge(self, user_id: str, query: str) -> MemoryRecallV2:
        """Recall with lazy merge of pending facts when threshold exceeded."""
        user = self._sanitize(user_id)
        user_dir = self.root / user
        if not user_dir.exists():
            return MemoryRecallV2(user_id=user)

        mem = UserMemoryData.load(user_dir)

        # Lazy merge if pending exceeds threshold
        if len(mem.pending_facts) > PENDING_MERGE_THRESHOLD:
            await self._merge_pending_facts(mem)
            mem.save(user_dir)

        # Archive old timeline
        self._archive_old_timeline(user_dir)

        # Delegate to regular recall
        return self.recall(user_id, query)

    async def _merge_pending_facts(self, mem: UserMemoryData) -> None:
        """Use LLM to semantically merge pending facts into confirmed facts."""
        existing_desc = "\n".join(f"- [{f.id}] {f.category}: {f.content}" for f in mem.facts if f.is_active())
        pending_desc = "\n".join(f"- {f.category}: {f.content}" for f in mem.pending_facts)

        llm = get_llm("orchestrator", temperature=0)
        prompt = f"""请合并用户记忆。规则：
1. 语义相同的条目合并为一条，保留信息量最大的表述
2. 同主题的更新版本替代旧版本
3. 全新信息保留
4. 返回合并后的完整 fact 列表（JSON 数组）

已确认的记忆：
{existing_desc or '(空)'}

待合并的新记忆：
{pending_desc}

返回格式：[{{"id": "保留原id或新id", "category": "...", "content": "...", "confidence": 0.0-1.0}}]"""

        try:
            response = await llm.ainvoke(prompt)
            content = response_text(response).strip()
            if "```json" in content:
                content = content.split("```json", 1)[1].split("```", 1)[0]
            elif "```" in content:
                content = content.split("```", 1)[1].split("```", 1)[0]
            merged = json.loads(content.strip())
            if isinstance(merged, list):
                mem.facts = [UserFact.from_dict(d) for d in merged if isinstance(d, dict)]
                mem.pending_facts = []
        except Exception:
            # Fallback: just move pending to confirmed without LLM merge
            mem.facts.extend(mem.pending_facts)
            mem.pending_facts = []

    def _archive_old_timeline(self, user_dir: Path) -> None:
        """Move hot timeline files older than HOT_RETENTION_DAYS to warm summaries."""
        hot_dir = user_dir / "timeline" / "hot"
        if not hot_dir.exists():
            return

        today = datetime.now(timezone.utc).date()
        for day_file in list(hot_dir.glob("*.md")):
            try:
                file_date = datetime.strptime(day_file.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            age = (today - file_date).days
            if age <= HOT_RETENTION_DAYS:
                continue

            summary = self._summarize_day_file(day_file)
            if summary:
                self._append_warm_timeline(user_dir, day_file.stem, summary)
            day_file.unlink()

    def _summarize_day_file(self, path: Path) -> str:
        """Rule-based day summary: count interactions, list intents."""
        text = path.read_text(encoding="utf-8")
        blocks = re.split(r"(?m)^## ", text)[1:]
        intents: list[str] = []
        for block in blocks:
            intent_match = re.search(r"intent:\s*(\S+)", block)
            if intent_match:
                intents.append(intent_match.group(1))
        if not intents:
            return ""
        unique_intents = ", ".join(sorted(set(intents)))
        return f"{path.stem}: {unique_intents} ({len(blocks)}次交互)"

    def _append_warm_timeline(self, user_dir: Path, date_str: str, summary: str) -> None:
        warm_dir = user_dir / "timeline" / "warm"
        warm_dir.mkdir(parents=True, exist_ok=True)
        month = date_str[:7]  # YYYY-MM
        warm_file = warm_dir / f"{month}.md"
        if not warm_file.exists():
            warm_file.write_text(f"# Warm Timeline {month}\n\n", encoding="utf-8")
        with warm_file.open("a", encoding="utf-8") as f:
            f.write(f"- {summary}\n")
