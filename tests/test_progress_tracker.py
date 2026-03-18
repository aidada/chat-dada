from __future__ import annotations

import unittest

from langchain_core.messages import AIMessage, ToolMessage

from progress_tracker import (
    MAX_KEY_FINDINGS,
    TRACKER_VERSION,
    ProgressTracker,
    extract_gaps_from_summary,
    extract_progress_from_tool_results,
)


class ProgressTrackerTests(unittest.TestCase):
    def test_record_search_success(self) -> None:
        tracker = ProgressTracker(original_query="test")
        tracker.record_search("GNSS accuracy", success=True)
        self.assertIn("GNSS accuracy", tracker.completed_searches)
        self.assertEqual(len(tracker.failed_searches), 0)

    def test_record_search_failure(self) -> None:
        tracker = ProgressTracker(original_query="test")
        tracker.record_search("broken query", success=False)
        self.assertIn("broken query", tracker.failed_searches)
        self.assertEqual(len(tracker.completed_searches), 0)

    def test_record_search_dedup(self) -> None:
        tracker = ProgressTracker(original_query="test")
        tracker.record_search("GNSS", success=True)
        tracker.record_search("GNSS", success=True)
        self.assertEqual(tracker.completed_searches.count("GNSS"), 1)

    def test_record_finding_fifo_cap(self) -> None:
        tracker = ProgressTracker(original_query="test")
        for i in range(MAX_KEY_FINDINGS + 5):
            tracker.record_finding(f"finding_{i}")
        self.assertEqual(len(tracker.key_findings_so_far), MAX_KEY_FINDINGS)
        # Oldest should be dropped
        self.assertEqual(tracker.key_findings_so_far[0], "finding_5")
        self.assertEqual(tracker.key_findings_so_far[-1], f"finding_{MAX_KEY_FINDINGS + 4}")

    def test_build_attention_block_format(self) -> None:
        tracker = ProgressTracker(original_query="GNSS NLOS detection")
        tracker.record_search("GNSS accuracy", success=True)
        tracker.record_finding("GNSS multipath affects accuracy")
        tracker.record_gap("Missing NLOS detection algorithms")
        block = tracker.build_attention_block()
        self.assertIn("---", block)
        self.assertIn("研究进度", block)
        self.assertIn("GNSS NLOS detection", block)
        self.assertIn("已完成搜索", block)
        self.assertIn("关键发现", block)
        self.assertIn("未覆盖缺口", block)
        self.assertIn("已失败搜索", block)

    def test_build_attention_block_empty(self) -> None:
        tracker = ProgressTracker()
        block = tracker.build_attention_block()
        self.assertIn("---", block)
        self.assertIn("(未设定)", block)
        # Should not raise

    def test_build_attention_block_truncation(self) -> None:
        tracker = ProgressTracker(original_query="test")
        # Add many long searches to exceed limit
        for i in range(35):
            tracker.record_search(f"very long search query number {i} " * 5, success=True)
        block = tracker.build_attention_block()
        self.assertLessEqual(len(block), 1500)

    def test_round_trip(self) -> None:
        tracker = ProgressTracker(original_query="GNSS", clarified_goal="Detect NLOS")
        tracker.record_search("GNSS accuracy", success=True)
        tracker.record_search("broken query", success=False)
        tracker.record_finding("GNSS multipath affects accuracy")
        tracker.record_gap("Missing algo")
        tracker.update_subtask("sub_1", "completed")

        data = tracker.to_dict()
        restored = ProgressTracker.from_dict(data)

        self.assertEqual(restored.original_query, "GNSS")
        self.assertEqual(restored.clarified_goal, "Detect NLOS")
        self.assertEqual(restored.completed_searches, ["GNSS accuracy"])
        self.assertEqual(restored.failed_searches, ["broken query"])
        self.assertEqual(restored.key_findings_so_far, ["GNSS multipath affects accuracy"])
        self.assertEqual(restored.remaining_gaps, ["Missing algo"])
        self.assertEqual(restored.subtasks_status, [{"id": "sub_1", "status": "completed"}])

    def test_extract_progress_from_tool_results(self) -> None:
        prev_ai = AIMessage(
            content="",
            tool_calls=[
                {"id": "call_1", "name": "web_search", "args": {"query": "GNSS accuracy"}},
                {"id": "call_2", "name": "web_search", "args": {"query": "broken"}},
            ],
        )
        tool_msgs = [
            ToolMessage(content="GNSS provides 3m accuracy in open sky.", tool_call_id="call_1", name="web_search"),
            ToolMessage(content="", tool_call_id="call_2", name="web_search"),
        ]

        completed, findings, failed = extract_progress_from_tool_results(tool_msgs, prev_ai)
        self.assertEqual(completed, ["GNSS accuracy"])
        self.assertEqual(len(findings), 1)
        self.assertIn("GNSS provides", findings[0])
        self.assertEqual(failed, ["broken"])

    def test_extract_gaps_from_summary(self) -> None:
        summary = (
            "## 已覆盖子主题\n"
            "已覆盖 GNSS 精度问题\n\n"
            "## 尚未覆盖的缺口\n"
            "- 缺少 NLOS 检测算法的对比实验数据\n"
            "- 尚未覆盖室内环境下的表现\n\n"
            "## 下一步建议\n"
            "- 搜索最新的 NLOS 检测方法\n"
            "- 补充室内定位精度数据\n"
        )
        gaps = extract_gaps_from_summary(summary)
        self.assertGreater(len(gaps), 0)
        self.assertLessEqual(len(gaps), 5)
        has_nlos = any("NLOS" in g for g in gaps)
        self.assertTrue(has_nlos)

    def test_extract_gaps_from_summary_empty(self) -> None:
        self.assertEqual(extract_gaps_from_summary(""), [])
        self.assertEqual(extract_gaps_from_summary("一切正常，没有问题。"), [])

    def test_resolve_gap(self) -> None:
        tracker = ProgressTracker(original_query="test")
        tracker.record_gap("缺少 NLOS 数据")
        tracker.record_gap("缺少室内测试")
        tracker.resolve_gap("NLOS")
        self.assertEqual(len(tracker.remaining_gaps), 1)
        self.assertNotIn("缺少 NLOS 数据", tracker.remaining_gaps)
        self.assertIn("缺少室内测试", tracker.remaining_gaps)

    def test_tracker_to_dict_includes_version(self) -> None:
        tracker = ProgressTracker(original_query="test")
        data = tracker.to_dict()
        self.assertEqual(data["_version"], TRACKER_VERSION)

    def test_tracker_from_dict_old_version_warns(self) -> None:
        data = {"original_query": "test"}
        tracker = ProgressTracker.from_dict(data)
        self.assertEqual(tracker.original_query, "test")

    def test_record_token_usage(self) -> None:
        tracker = ProgressTracker(original_query="test")
        tracker.record_token_usage(100, 50)
        tracker.record_token_usage(200, 80)
        self.assertEqual(tracker.total_input_tokens, 300)
        self.assertEqual(tracker.total_output_tokens, 130)

    def test_attention_block_shows_tokens(self) -> None:
        tracker = ProgressTracker(original_query="test")
        tracker.record_token_usage(1000, 500)
        block = tracker.build_attention_block()
        self.assertIn("累计 token", block)
        self.assertIn("1000", block)

    def test_token_round_trip(self) -> None:
        tracker = ProgressTracker(original_query="test")
        tracker.record_token_usage(100, 50)
        data = tracker.to_dict()
        restored = ProgressTracker.from_dict(data)
        self.assertEqual(restored.total_input_tokens, 100)
        self.assertEqual(restored.total_output_tokens, 50)


if __name__ == "__main__":
    unittest.main()
