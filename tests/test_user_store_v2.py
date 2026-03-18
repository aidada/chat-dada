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
