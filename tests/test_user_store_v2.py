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
        categories = [f.category for f in mem.pending_facts]
        self.assertIn("identity", categories)
        self.assertIn("preference", categories)
        self.assertIn("working_style", categories)
        self.assertIn("constraint", categories)
        # Projects should be created
        self.assertTrue(len(mem.projects) > 0)
        # Old profile.md should be renamed to profile.md.bak
        self.assertTrue((user_dir / "profile.md.bak").exists())

    def test_no_migrate_if_already_migrated(self) -> None:
        user_dir = self.root / "test_user"
        user_dir.mkdir()
        # Write both old profile.md and new facts.json
        (user_dir / "profile.md").write_text("# old\n## Identity\n- test\n", encoding="utf-8")
        (user_dir / "facts.json").write_text("[]", encoding="utf-8")

        self.store._migrate_if_needed(user_dir)
        # profile.md should NOT be renamed (already migrated)
        self.assertTrue((user_dir / "profile.md").exists())
        self.assertFalse((user_dir / "profile.md.bak").exists())


if __name__ == "__main__":
    unittest.main()
