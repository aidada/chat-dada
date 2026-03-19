# 用户记忆系统重设计实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将用户记忆从扁平文本列表改为结构化实体（UserFact + Project），实现语义合并、项目生命周期、Timeline 分层归档、离线回归检测。

**Architecture:** UserFact 和 Project 取代当前 6 个 profile section。新 fact 追加到 pending 队列立即可见，累积超过 8 条时惰性触发 LLM 语义合并。Timeline 分 Hot(7天)/Warm(90天)/Cold(月度总结) 三级，控制存储增长。

**Tech Stack:** Python 3.13, dataclasses, JSON 持久化, LLM (orchestrator role), pytest

---

## 前置说明

### 当前文件
- 源码：`storage/user_store.py`（451 行）
- 调用方：`orchestrator/runner.py:59-89`
- 数据：`data/memory/<user_id>/profile.md` + `timeline/` + `summaries/`
- 测试：暂无专门测试文件

### 实际数据问题（profile.md 示例）
```
## Projects
- 调研课题：《利用 GNSS 多普勒频移可以反演 NLOS 信号到达方向》
- 正在调研课题《利用 GNSS 多普勒频移可以反演 NLOS 信号到达方向》     ← 重复
- 正在调研课题：《利用 GNSS 多普勒频移可以反演 NLOS 信号到达方向》  ← 重复
- 调研课题：利用 GNSS 多普勒频移反演 NLOS 信号到达方向...            ← 同义变体
```

12 条 Projects 全是 GNSS 论文的微小变体，casefold 去重不生效。

### 迁移策略
旧 `profile.md` → 新 `facts.json` + `projects.json` 的数据迁移在 Task 4 实现，向后兼容。

---

### Task 1: 定义数据模型 + 基础持久化

**Files:**
- Create: `storage/user_models.py`
- Create: `tests/test_user_models.py`

**Step 1: 写测试**

创建 `tests/test_user_models.py`：

```python
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from storage.user_models import UserFact, Project, UserMemoryData


class UserFactTests(unittest.TestCase):
    def test_fact_round_trip(self) -> None:
        fact = UserFact(
            id="f1",
            category="identity",
            content="用户是博士生，研究方向是 GNSS",
            confidence=0.8,
            first_seen="2026-03-01T00:00:00+00:00",
            last_confirmed="2026-03-18T00:00:00+00:00",
        )
        data = fact.to_dict()
        restored = UserFact.from_dict(data)
        self.assertEqual(restored.id, "f1")
        self.assertEqual(restored.confidence, 0.8)
        self.assertEqual(restored.content, fact.content)

    def test_fact_defaults(self) -> None:
        fact = UserFact(id="f1", category="preference", content="喜欢简洁输出")
        self.assertEqual(fact.confidence, 0.5)
        self.assertIsNone(fact.superseded_by)


class ProjectTests(unittest.TestCase):
    def test_project_round_trip(self) -> None:
        proj = Project(
            id="p1",
            name="GNSS NLOS 检测论文",
            status="active",
            description="利用多普勒频移反演 NLOS 信号到达方向",
            created_at="2026-03-01T00:00:00+00:00",
            updated_at="2026-03-18T00:00:00+00:00",
        )
        data = proj.to_dict()
        restored = Project.from_dict(data)
        self.assertEqual(restored.id, "p1")
        self.assertEqual(restored.status, "active")

    def test_project_is_stale(self) -> None:
        old_date = "2026-02-01T00:00:00+00:00"
        proj = Project(id="p1", name="Old project", status="active", updated_at=old_date)
        self.assertTrue(proj.is_stale(stale_days=14))

    def test_project_not_stale_if_recent(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        proj = Project(id="p1", name="New project", status="active", updated_at=now)
        self.assertFalse(proj.is_stale(stale_days=14))


class UserMemoryDataTests(unittest.TestCase):
    def test_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp)
            mem = UserMemoryData()
            mem.facts.append(UserFact(id="f1", category="identity", content="test"))
            mem.projects.append(Project(id="p1", name="Test Project"))
            mem.pending_facts.append(UserFact(id="f2", category="preference", content="pending"))
            mem.save(user_dir)

            loaded = UserMemoryData.load(user_dir)
            self.assertEqual(len(loaded.facts), 1)
            self.assertEqual(len(loaded.projects), 1)
            self.assertEqual(len(loaded.pending_facts), 1)
            self.assertEqual(loaded.facts[0].content, "test")

    def test_load_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loaded = UserMemoryData.load(Path(tmp))
            self.assertEqual(len(loaded.facts), 0)
            self.assertEqual(len(loaded.projects), 0)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_user_models.py -v
```

Expected: FAIL — `storage.user_models` 不存在。

**Step 3: 实现数据模型**

创建 `storage/user_models.py`：

```python
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
```

**Step 4: 运行测试**

```bash
python -m pytest tests/test_user_models.py -v
```

Expected: ALL PASS

**Step 5: Commit**

```bash
git add storage/user_models.py tests/test_user_models.py
git commit -m "feat: add UserFact/Project data models with JSON persistence"
```

---

### Task 2: 重写 recall() — 结构化实体 + 离线回归检测

**Files:**
- Create: `storage/user_store_v2.py`（新文件，不破坏旧版）
- Create: `tests/test_user_store_v2.py`

**Step 1: 写测试**

创建 `tests/test_user_store_v2.py`：

```python
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from storage.user_models import UserFact, Project, UserMemoryData
from storage.user_store_v2 import MemoryStoreV2, MemoryRecallV2

STALE_DAYS = 14


class RecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = MemoryStoreV2(root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_recall_empty_user(self) -> None:
        recall = self.store.recall("new_user", "hello")
        self.assertFalse(recall.has_content())

    def test_recall_returns_facts_and_projects(self) -> None:
        user_dir = self.root / "test_user"
        mem = UserMemoryData()
        mem.facts.append(UserFact(id="f1", category="identity", content="博士生", confidence=0.8))
        mem.projects.append(Project(id="p1", name="GNSS论文", status="active"))
        mem.meta["last_seen"] = datetime.now(timezone.utc).isoformat()
        mem.save(user_dir)

        recall = self.store.recall("test_user", "GNSS研究")
        self.assertTrue(recall.has_content())
        self.assertEqual(len(recall.facts), 1)
        self.assertEqual(len(recall.active_projects), 1)
        self.assertEqual(recall.facts[0].content, "博士生")

    def test_recall_includes_pending_facts(self) -> None:
        user_dir = self.root / "test_user"
        mem = UserMemoryData()
        mem.facts.append(UserFact(id="f1", category="identity", content="confirmed"))
        mem.pending_facts.append(UserFact(id="f2", category="preference", content="pending"))
        mem.meta["last_seen"] = datetime.now(timezone.utc).isoformat()
        mem.save(user_dir)

        recall = self.store.recall("test_user", "anything")
        self.assertEqual(len(recall.facts), 2)

    def test_recall_detects_offline_return(self) -> None:
        user_dir = self.root / "test_user"
        mem = UserMemoryData()
        mem.facts.append(UserFact(id="f1", category="identity", content="test", confidence=1.0))
        mem.projects.append(Project(id="p1", name="Old project", status="active"))
        mem.meta["last_seen"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        mem.save(user_dir)

        recall = self.store.recall("test_user", "hello")
        # Facts should have decayed confidence
        self.assertLess(recall.facts[0].confidence, 1.0)
        # Projects should be marked stale
        self.assertEqual(len(recall.stale_projects), 1)
        self.assertEqual(len(recall.active_projects), 0)
        # Should have a note
        self.assertTrue(len(recall.notes) > 0)

    def test_recall_sorts_facts_by_confidence(self) -> None:
        user_dir = self.root / "test_user"
        mem = UserMemoryData()
        mem.facts.append(UserFact(id="f1", category="identity", content="low", confidence=0.3))
        mem.facts.append(UserFact(id="f2", category="identity", content="high", confidence=0.9))
        mem.meta["last_seen"] = datetime.now(timezone.utc).isoformat()
        mem.save(user_dir)

        recall = self.store.recall("test_user", "test")
        self.assertEqual(recall.facts[0].content, "high")

    def test_recall_filters_superseded_facts(self) -> None:
        user_dir = self.root / "test_user"
        mem = UserMemoryData()
        mem.facts.append(UserFact(id="f1", category="identity", content="old", superseded_by="f2"))
        mem.facts.append(UserFact(id="f2", category="identity", content="new"))
        mem.meta["last_seen"] = datetime.now(timezone.utc).isoformat()
        mem.save(user_dir)

        recall = self.store.recall("test_user", "test")
        contents = [f.content for f in recall.facts]
        self.assertNotIn("old", contents)
        self.assertIn("new", contents)


class RecallToPromptTests(unittest.TestCase):
    def test_to_prompt_with_all_sections(self) -> None:
        recall = MemoryRecallV2(
            user_id="test",
            facts=[UserFact(id="f1", category="identity", content="博士生", confidence=0.9)],
            active_projects=[Project(id="p1", name="GNSS论文", description="研究多径")],
            stale_projects=[Project(id="p2", name="旧项目", updated_at="2026-01-01T00:00:00+00:00")],
            notes=["⚠ 用户已 30 天未互动。"],
        )
        prompt = recall.to_prompt()
        self.assertIn("博士生", prompt)
        self.assertIn("GNSS论文", prompt)
        self.assertIn("旧项目", prompt)
        self.assertIn("30 天未互动", prompt)

    def test_to_prompt_empty(self) -> None:
        recall = MemoryRecallV2(user_id="test")
        self.assertEqual(recall.to_prompt(), "")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_user_store_v2.py -v
```

Expected: FAIL — `storage.user_store_v2` 不存在。

**Step 3: 实现 MemoryStoreV2 的 recall()**

创建 `storage/user_store_v2.py`：

```python
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
```

**Step 4: 运行测试**

```bash
python -m pytest tests/test_user_store_v2.py -v
```

Expected: ALL PASS

**Step 5: Commit**

```bash
git add storage/user_store_v2.py tests/test_user_store_v2.py
git commit -m "feat: MemoryStoreV2 recall with structured facts, projects, offline detection"
```

---

### Task 3: 实现 remember() — fact 提取 + pending 队列

**Files:**
- Modify: `storage/user_store_v2.py`
- Modify: `tests/test_user_store_v2.py`

**Step 1: 写测试**

在 `tests/test_user_store_v2.py` 添加新 class：

```python
class RememberTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = MemoryStoreV2(root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_remember_creates_pending_facts(self) -> None:
        from unittest.mock import patch, AsyncMock
        from langchain_core.messages import AIMessage

        class _MockLLM:
            async def ainvoke(self, prompt):
                return AIMessage(content='[{"category": "identity", "content": "博士生"}]')

        with patch("storage.user_store_v2.get_llm", return_value=_MockLLM()):
            await self.store.remember("test_user", "我是博士生", "好的", intent="chat")

        mem = UserMemoryData.load(self.root / "test_user")
        self.assertEqual(len(mem.pending_facts), 1)
        self.assertEqual(mem.pending_facts[0].category, "identity")
        self.assertIn("last_seen", mem.meta)

    async def test_remember_appends_timeline(self) -> None:
        from unittest.mock import patch
        from langchain_core.messages import AIMessage

        class _MockLLM:
            async def ainvoke(self, prompt):
                return AIMessage(content='[]')

        with patch("storage.user_store_v2.get_llm", return_value=_MockLLM()):
            await self.store.remember("test_user", "hello", "hi", intent="chat")

        hot_dir = self.root / "test_user" / "timeline" / "hot"
        self.assertTrue(hot_dir.exists())
        day_files = list(hot_dir.glob("*.md"))
        self.assertEqual(len(day_files), 1)

    async def test_remember_extracts_project(self) -> None:
        from unittest.mock import patch
        from langchain_core.messages import AIMessage

        class _MockLLM:
            async def ainvoke(self, prompt):
                return AIMessage(content='[{"category": "project", "content": "GNSS NLOS 检测论文"}]')

        with patch("storage.user_store_v2.get_llm", return_value=_MockLLM()):
            await self.store.remember("test_user", "我在做GNSS论文", "好的", intent="research")

        mem = UserMemoryData.load(self.root / "test_user")
        self.assertEqual(len(mem.projects), 1)
        self.assertEqual(mem.projects[0].name, "GNSS NLOS 检测论文")
```

**Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_user_store_v2.py::RememberTests -v
```

Expected: FAIL

**Step 3: 实现 remember()**

在 `storage/user_store_v2.py` 的 `MemoryStoreV2` 中添加：

```python
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
                # Create or update project
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
                    p.status = "active"  # re-activate

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
        from core.models import get_llm, response_text
        import json

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
```

**Step 4: 运行测试**

```bash
python -m pytest tests/test_user_store_v2.py -v
```

Expected: ALL PASS

**Step 5: Commit**

```bash
git add storage/user_store_v2.py tests/test_user_store_v2.py
git commit -m "feat: MemoryStoreV2 remember with fact extraction and project lifecycle"
```

---

### Task 4: 惰性合并 + Timeline 归档

**Files:**
- Modify: `storage/user_store_v2.py`
- Modify: `tests/test_user_store_v2.py`

**Step 1: 写测试**

```python
class MergeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = MemoryStoreV2(root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_merge_triggered_when_pending_exceeds_threshold(self) -> None:
        from unittest.mock import patch
        from langchain_core.messages import AIMessage

        user_dir = self.root / "test_user"
        mem = UserMemoryData()
        # Add 9 pending facts (over threshold of 8)
        for i in range(9):
            mem.pending_facts.append(UserFact(id=f"f{i}", category="identity", content=f"fact {i}"))
        mem.meta["last_seen"] = datetime.now(timezone.utc).isoformat()
        mem.save(user_dir)

        # LLM should be called to merge
        class _MergeLLM:
            async def ainvoke(self, prompt):
                # Return deduplicated facts
                return AIMessage(content='[{"id": "merged_1", "category": "identity", "content": "merged fact", "confidence": 0.8}]')

        with patch("storage.user_store_v2.get_llm", return_value=_MergeLLM()):
            recall = await self.store.recall_with_merge("test_user", "test")

        # After merge, pending should be cleared
        mem_after = UserMemoryData.load(user_dir)
        self.assertEqual(len(mem_after.pending_facts), 0)
        self.assertTrue(len(mem_after.facts) > 0)


class TimelineArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = MemoryStoreV2(root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_archive_old_timeline_to_warm(self) -> None:
        user_dir = self.root / "test_user"
        hot_dir = user_dir / "timeline" / "hot"
        hot_dir.mkdir(parents=True)

        # Create an 8-day old file
        old_date = datetime.now(timezone.utc) - timedelta(days=8)
        old_file = hot_dir / f"{old_date.strftime('%Y-%m-%d')}.md"
        old_file.write_text(
            f"# Timeline {old_date.strftime('%Y-%m-%d')}\n\n"
            f"## {old_date.isoformat()}\n- intent: research\n\n### User\nGNSS研究\n\n### Assistant\n结果\n\n"
            f"## {old_date.isoformat()}\n- intent: chat\n\n### User\n你好\n\n### Assistant\n你好\n\n",
            encoding="utf-8",
        )

        # Create a recent file (should not be archived)
        today = datetime.now(timezone.utc)
        today_file = hot_dir / f"{today.strftime('%Y-%m-%d')}.md"
        today_file.write_text("# Today\n\n## entry\nrecent\n", encoding="utf-8")

        self.store._archive_old_timeline(user_dir)

        # Old file should be removed
        self.assertFalse(old_file.exists())
        # Today file should remain
        self.assertTrue(today_file.exists())
        # Warm file should exist
        warm_dir = user_dir / "timeline" / "warm"
        warm_files = list(warm_dir.glob("*.md"))
        self.assertTrue(len(warm_files) > 0)
```

**Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_user_store_v2.py::MergeTests tests/test_user_store_v2.py::TimelineArchiveTests -v
```

Expected: FAIL

**Step 3: 实现惰性合并**

在 `storage/user_store_v2.py` 添加：

```python
PENDING_MERGE_THRESHOLD = 8

class MemoryStoreV2:
    # ... existing methods ...

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
        from core.models import get_llm, response_text
        import json

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
```

**Step 4: 实现 timeline 归档**

```python
    HOT_RETENTION_DAYS = 7

    def _archive_old_timeline(self, user_dir: Path) -> None:
        """Move hot timeline files older than 7 days to warm summaries."""
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
            if age <= self.HOT_RETENTION_DAYS:
                continue

            # Summarize the day (rule-based, no LLM)
            summary = self._summarize_day_file(day_file)
            if summary:
                self._append_warm_timeline(user_dir, day_file.stem, summary)
            day_file.unlink()

    def _summarize_day_file(self, path: Path) -> str:
        """Rule-based day summary: count interactions, list intents."""
        import re
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
```

**Step 5: 运行测试**

```bash
python -m pytest tests/test_user_store_v2.py -v
```

Expected: ALL PASS

**Step 6: Commit**

```bash
git add storage/user_store_v2.py tests/test_user_store_v2.py
git commit -m "feat: lazy fact merge + hot/warm timeline archiving"
```

---

### Task 5: 旧数据迁移 + 接入 orchestrator

**Files:**
- Modify: `storage/user_store_v2.py`（添加迁移方法）
- Modify: `orchestrator/runner.py`（切换到 V2）
- Modify: `tests/test_user_store_v2.py`

**Step 1: 写迁移测试**

```python
class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = MemoryStoreV2(root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_migrate_from_profile_md(self) -> None:
        user_dir = self.root / "test_user"
        user_dir.mkdir()
        # Write old-format profile.md
        (user_dir / "profile.md").write_text(
            "# User Profile Memory\n\n"
            "## Identity\n- 博士生\n\n"
            "## Preferences\n- 喜欢简洁输出\n\n"
            "## Projects\n- GNSS NLOS 检测论文\n- 正在做 GNSS 论文\n\n"
            "## Working Style\n- 喜欢研究报告形式\n\n"
            "## Constraints\n- 可用50个接收机数据\n\n"
            "## Open Loops\n- 需要完成文献综述\n\n",
            encoding="utf-8",
        )

        self.store._migrate_if_needed(user_dir)

        mem = UserMemoryData.load(user_dir)
        # Facts should be created from old sections
        categories = [f.category for f in mem.facts]
        self.assertIn("identity", categories)
        self.assertIn("preference", categories)
        self.assertIn("working_style", categories)
        self.assertIn("constraint", categories)
        # Projects should be created
        self.assertTrue(len(mem.projects) > 0)
        # Old profile.md should be renamed to profile.md.bak
        self.assertTrue((user_dir / "profile.md.bak").exists())
```

**Step 2: 实现迁移**

在 `storage/user_store_v2.py` 添加：

```python
    CATEGORY_MAP = {
        "Identity": "identity",
        "Preferences": "preference",
        "Working Style": "working_style",
        "Constraints": "constraint",
        "Open Loops": "constraint",  # open loops → constraints/todos
    }

    def _migrate_if_needed(self, user_dir: Path) -> None:
        """Migrate from old profile.md format to new structured format."""
        profile_path = user_dir / "profile.md"
        facts_path = user_dir / "facts.json"

        if not profile_path.exists() or facts_path.exists():
            return  # Already migrated or no old data

        mem = UserMemoryData()

        # Parse old profile.md
        current_section: str | None = None
        for line in profile_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                current_section = stripped[3:].strip()
                continue
            if current_section and stripped.startswith("- ") and stripped != "-":
                content = stripped[2:].strip()
                if not content:
                    continue

                if current_section == "Projects":
                    self._upsert_project(mem, content, datetime.now(timezone.utc))
                elif current_section in self.CATEGORY_MAP:
                    mem.pending_facts.append(UserFact(
                        category=self.CATEGORY_MAP[current_section],
                        content=content,
                    ))

        # Move old timeline to hot/
        old_timeline = user_dir / "timeline"
        if old_timeline.exists():
            hot_dir = user_dir / "timeline" / "hot"
            hot_dir.mkdir(parents=True, exist_ok=True)
            for year_dir in old_timeline.iterdir():
                if year_dir.is_dir() and year_dir.name != "hot" and year_dir.name != "warm":
                    for month_dir in year_dir.iterdir():
                        if month_dir.is_dir():
                            for md_file in month_dir.glob("*.md"):
                                md_file.rename(hot_dir / md_file.name)

        mem.save(user_dir)
        profile_path.rename(user_dir / "profile.md.bak")

    def recall(self, user_id: str, query: str) -> MemoryRecallV2:
        user = self._sanitize(user_id)
        user_dir = self.root / user

        if not user_dir.exists():
            return MemoryRecallV2(user_id=user)

        # Auto-migrate on first access
        self._migrate_if_needed(user_dir)

        # ... rest of existing recall logic ...
```

**Step 3: 修改 orchestrator/runner.py 切换到 V2**

```python
# 将
from storage.user_store import get_memory_store
# 改为
from storage.user_store_v2 import MemoryStoreV2

# 将
memory_store = get_memory_store()
# 改为
memory_store = MemoryStoreV2()

# recall 调用改为 recall_with_merge（异步合并）：
memory_recall = await memory_store.recall_with_merge(user_id, task)
```

同时更新 `on_step` 日志格式适配 `MemoryRecallV2` 的字段。

**Step 4: 运行全量测试**

```bash
python -m pytest tests/ -v
```

Expected: ALL PASS

**Step 5: Commit**

```bash
git add storage/user_store_v2.py orchestrator/runner.py tests/test_user_store_v2.py
git commit -m "feat: data migration from profile.md + integrate MemoryStoreV2 into orchestrator"
```

---

### Task 6: 清理 + 全量回归

**Files:**
- 可选删除: `storage/user_store.py`（旧版本，保留为 backup 或直接删除）

**Step 1: 确认无遗留引用**

```bash
grep -rn "user_store\b" --include="*.py" . | grep -v "user_store_v2" | grep -v "test_user" | grep -v "__pycache__"
```

如果还有引用 `user_store`（非 v2）的文件，逐一更新。

**Step 2: 全量回归**

```bash
python -m pytest tests/ -v
```

Expected: ALL PASS

**Step 3: Commit**

```bash
git add -A
git commit -m "chore: memory system v2 migration complete"
```
