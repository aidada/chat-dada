"""Structured data models for user memory."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class UserFact:
    id: str = ""
    category: str = ""          # identity | preference | constraint | working_style
    content: str = ""
    confidence: float = 0.5     # 0.0-1.0
    first_seen: str = ""
    last_confirmed: str = ""
    superseded_by: str | None = None

    def __post_init__(self):
        if not self.id:
            self.id = _new_id()
        if not self.first_seen:
            self.first_seen = _now_iso()
        if not self.last_confirmed:
            self.last_confirmed = self.first_seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "content": self.content,
            "confidence": self.confidence,
            "first_seen": self.first_seen,
            "last_confirmed": self.last_confirmed,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserFact:
        return cls(
            id=data.get("id", ""),
            category=data.get("category", ""),
            content=data.get("content", ""),
            confidence=data.get("confidence", 0.5),
            first_seen=data.get("first_seen", ""),
            last_confirmed=data.get("last_confirmed", ""),
            superseded_by=data.get("superseded_by"),
        )

    def is_active(self) -> bool:
        return self.superseded_by is None


@dataclass
class Project:
    id: str = ""
    name: str = ""
    status: str = "active"      # active | stale | completed | paused | abandoned
    description: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    related_tasks: list[str] = field(default_factory=list)
    key_findings: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.id:
            self.id = _new_id()
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "related_tasks": list(self.related_tasks),
            "key_findings": list(self.key_findings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Project:
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            status=data.get("status", "active"),
            description=data.get("description", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            completed_at=data.get("completed_at"),
            related_tasks=list(data.get("related_tasks", [])),
            key_findings=list(data.get("key_findings", [])),
        )

    def is_stale(self, stale_days: int = 14) -> bool:
        if self.status != "active":
            return False
        try:
            updated = datetime.fromisoformat(self.updated_at)
            gap = (datetime.now(timezone.utc) - updated).days
            return gap > stale_days
        except (ValueError, TypeError):
            return False


@dataclass
class UserMemoryData:
    """Container for all user memory entities, with JSON persistence."""
    facts: list[UserFact] = field(default_factory=list)
    pending_facts: list[UserFact] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def save(self, user_dir: Path) -> None:
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "facts.json").write_text(
            json.dumps([f.to_dict() for f in self.facts], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (user_dir / "pending_facts.json").write_text(
            json.dumps([f.to_dict() for f in self.pending_facts], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (user_dir / "projects.json").write_text(
            json.dumps([p.to_dict() for p in self.projects], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.meta["updated_at"] = _now_iso()
        (user_dir / "meta.json").write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, user_dir: Path) -> UserMemoryData:
        mem = cls()
        if (user_dir / "facts.json").exists():
            mem.facts = [UserFact.from_dict(d) for d in json.loads((user_dir / "facts.json").read_text(encoding="utf-8"))]
        if (user_dir / "pending_facts.json").exists():
            mem.pending_facts = [UserFact.from_dict(d) for d in json.loads((user_dir / "pending_facts.json").read_text(encoding="utf-8"))]
        if (user_dir / "projects.json").exists():
            mem.projects = [Project.from_dict(d) for d in json.loads((user_dir / "projects.json").read_text(encoding="utf-8"))]
        if (user_dir / "meta.json").exists():
            mem.meta = json.loads((user_dir / "meta.json").read_text(encoding="utf-8"))
        return mem
