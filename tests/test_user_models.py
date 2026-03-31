from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from infra.storage.user_models import UserFact, Project, UserMemoryData


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
