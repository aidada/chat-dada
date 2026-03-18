"""Structured user memory store with semantic facts and project lifecycle."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from storage.user_models import UserFact, Project, UserMemoryData

OFFLINE_THRESHOLD_DAYS = 14
CONFIDENCE_DECAY = 0.7
MAX_RECALL_FACTS = 10


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
