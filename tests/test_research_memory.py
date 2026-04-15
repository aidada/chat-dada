from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent.capabilities.memory import ResearchMemory, _sanitize_tool_name


class ResearchMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.mem = ResearchMemory("test_task_001", root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_init_creates_structure(self) -> None:
        self.mem.init("量子计算", "default")
        self.assertTrue((self.mem.task_dir / "findings").is_dir())
        self.assertTrue((self.mem.task_dir / "summaries").is_dir())
        self.assertTrue((self.mem.task_dir / "checkpoints").is_dir())
        self.assertTrue((self.mem.task_dir / "meta.json").exists())

    def test_meta_json_content(self) -> None:
        self.mem.init("量子计算", "academic")
        meta = json.loads((self.mem.task_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["task_id"], "test_task_001")
        self.assertEqual(meta["query"], "量子计算")
        self.assertEqual(meta["report_profile"], "academic")
        self.assertIn("created_at", meta)

    def test_save_load_finding(self) -> None:
        self.mem.init("q", "default")
        self.mem.save_finding(1, "web_search", "GNSS", "Search results here", ["https://a.com"])
        loaded = self.mem.load_finding(1, "web_search")
        self.assertIsNotNone(loaded)
        self.assertIn("Search results here", loaded)
        self.assertIn("https://a.com", loaded)

    def test_list_findings_sorted(self) -> None:
        self.mem.init("q", "default")
        self.mem.save_finding(3, "academic_search", "q", "r3", [])
        self.mem.save_finding(1, "web_search", "q", "r1", [])
        self.mem.save_finding(2, "web_search", "q", "r2", [])
        paths = self.mem.list_findings()
        names = [p.name for p in paths]
        self.assertEqual(names[0], "step_01_web_search.md")
        self.assertEqual(names[-1], "step_03_academic_search.md")

    def test_save_summary_both_files(self) -> None:
        self.mem.init("q", "default")
        self.mem.save_summary(5, "中期总结内容")
        step_file = self.mem.task_dir / "summaries" / "step_05.md"
        latest_file = self.mem.task_dir / "summaries" / "latest.md"
        self.assertTrue(step_file.exists())
        self.assertTrue(latest_file.exists())
        self.assertEqual(step_file.read_text(encoding="utf-8"), "中期总结内容")
        self.assertEqual(latest_file.read_text(encoding="utf-8"), "中期总结内容")

    def test_load_latest_summary(self) -> None:
        self.mem.init("q", "default")
        self.mem.save_summary(3, "旧总结")
        self.mem.save_summary(7, "新总结")
        latest = self.mem.load_latest_summary()
        self.assertEqual(latest, "新总结")

    def test_save_load_checkpoint(self) -> None:
        self.mem.init("q", "default")
        state = {"step_count": 5, "findings": "data"}
        self.mem.save_checkpoint(5, state)
        loaded = self.mem.load_checkpoint(5)
        self.assertEqual(loaded["step_count"], 5)
        self.assertEqual(loaded["findings"], "data")
        self.assertIn("_checkpoint_version", loaded)

    def test_load_latest_checkpoint(self) -> None:
        self.mem.init("q", "default")
        self.mem.save_checkpoint(3, {"step": 3})
        self.mem.save_checkpoint(7, {"step": 7})
        latest = self.mem.load_checkpoint()
        self.assertEqual(latest["step"], 7)

    def test_load_checkpoint_none(self) -> None:
        self.mem.init("q", "default")
        self.assertIsNone(self.mem.load_checkpoint())

    def test_save_final_report(self) -> None:
        self.mem.init("q", "default")
        self.mem.save_final_report("# Final Report\n\nContent here.")
        path = self.mem.task_dir / "final_report.md"
        self.assertTrue(path.exists())
        self.assertIn("Final Report", path.read_text(encoding="utf-8"))

    def test_sanitize_tool_name(self) -> None:
        self.assertEqual(_sanitize_tool_name("web_search"), "web_search")
        self.assertEqual(_sanitize_tool_name("tool/with spaces"), "tool_with_spaces")
        self.assertEqual(_sanitize_tool_name("a" * 100), "a" * 40)
        self.assertEqual(_sanitize_tool_name("  "), "unknown")

    def test_load_meta(self) -> None:
        self.mem.init("量子计算", "academic")
        meta = self.mem.load_meta()
        self.assertIsNotNone(meta)
        self.assertEqual(meta["query"], "量子计算")
        self.assertEqual(meta["report_profile"], "academic")

    def test_load_meta_missing(self) -> None:
        # Don't call init — meta.json doesn't exist
        result = self.mem.load_meta()
        self.assertIsNone(result)

    def test_checkpoint_version_roundtrip(self) -> None:
        from agent.capabilities.memory import CHECKPOINT_VERSION
        self.mem.init("q", "default")
        state = {"step_count": 5, "findings": "test"}
        self.mem.save_checkpoint(5, state)
        loaded = self.mem.load_checkpoint(5)
        self.assertEqual(loaded["_checkpoint_version"], CHECKPOINT_VERSION)

    def test_list_tasks(self) -> None:
        mem1 = ResearchMemory("task_a", root=self.root)
        mem1.init("query A", "default")
        mem2 = ResearchMemory("task_b", root=self.root)
        mem2.init("query B", "default")
        tasks = ResearchMemory.list_tasks(root=self.root)
        task_ids = [t.get("task_id", "") for t in tasks]
        self.assertIn("task_a", task_ids)
        self.assertIn("task_b", task_ids)

    def test_cleanup_removes_findings(self) -> None:
        self.mem.init("q", "default")
        self.mem.save_finding(1, "web_search", "q", "data", [])
        self.mem.save_checkpoint(1, {"step": 1})
        self.mem.cleanup()
        self.assertFalse((self.mem.task_dir / "findings").exists())
        self.assertFalse((self.mem.task_dir / "checkpoints").exists())

    def test_cleanup_keeps_final_report(self) -> None:
        self.mem.init("q", "default")
        self.mem.save_final_report("# Report")
        self.mem.cleanup(keep_final_report=True)
        self.assertTrue((self.mem.task_dir / "final_report.md").exists())

    def test_cleanup_old_tasks(self) -> None:
        import json
        from datetime import timedelta
        # Create a task with old timestamp
        old_mem = ResearchMemory("old_task", root=self.root)
        old_mem.init("old query", "default")
        # Manually backdate the created_at
        meta_path = old_mem.task_dir / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        from datetime import datetime, timezone
        meta["created_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        # Create a recent task
        new_mem = ResearchMemory("new_task", root=self.root)
        new_mem.init("new query", "default")

        count = ResearchMemory.cleanup_old_tasks(max_age_days=30, root=self.root)
        self.assertEqual(count, 1)
        self.assertFalse((self.root / "old_task").exists())
        self.assertTrue((self.root / "new_task").exists())


if __name__ == "__main__":
    unittest.main()
