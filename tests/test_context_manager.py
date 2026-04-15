from __future__ import annotations

import unittest

from langchain_core.messages import ToolMessage

from agent.capabilities.context_manager import (
    FindingEntry,
    ResearchContext,
    _extract_urls,
)


class FindingEntryTests(unittest.TestCase):
    def test_finding_entry_round_trip(self) -> None:
        entry = FindingEntry(
            step=1,
            tool_name="web_search",
            query="GNSS",
            raw_content="raw data",
            source_urls=["https://example.com"],
            evidence_strength="medium",
            key_claims=["claim1"],
        )
        restored = FindingEntry.from_dict(entry.to_dict())
        self.assertEqual(restored.step, 1)
        self.assertEqual(restored.tool_name, "web_search")
        self.assertEqual(restored.raw_content, "raw data")
        self.assertEqual(restored.source_urls, ["https://example.com"])
        self.assertEqual(restored.key_claims, ["claim1"])


class ResearchContextTests(unittest.IsolatedAsyncioTestCase):
    def test_add_entry_updates_step(self) -> None:
        ctx = ResearchContext()
        ctx.add_entry(FindingEntry(step=3, tool_name="t", query="q", raw_content="x"))
        self.assertEqual(ctx.current_step, 3)
        ctx.add_entry(FindingEntry(step=1, tool_name="t", query="q", raw_content="x"))
        self.assertEqual(ctx.current_step, 3)  # should not decrease

    def test_merge_tool_results_extracts_urls(self) -> None:
        ctx = ResearchContext()
        msgs = [
            ToolMessage(
                content="See https://example.com/page and https://other.org/doc",
                tool_call_id="call_1",
                name="web_search",
            )
        ]
        entries = ctx.merge_tool_results(msgs, step=1)
        self.assertEqual(len(entries), 1)
        self.assertIn("https://example.com/page", entries[0].source_urls)
        self.assertIn("https://other.org/doc", entries[0].source_urls)

    async def test_trigger_compression_compacts_old(self) -> None:
        ctx = ResearchContext()
        # Add a large entry at step 1
        ctx.add_entry(
            FindingEntry(step=1, tool_name="t", query="q", raw_content="A" * 9000)
        )
        # Add a small entry at step 3
        ctx.add_entry(
            FindingEntry(step=3, tool_name="t2", query="q", raw_content="B" * 100)
        )
        await ctx.trigger_compression(step=3)

        old_entry = ctx.entries[0]
        self.assertTrue(old_entry.compact_content)
        self.assertEqual(old_entry.raw_content, "")

        # Recent entry should be untouched
        recent = ctx.entries[1]
        self.assertFalse(recent.compact_content)
        self.assertEqual(recent.raw_content, "B" * 100)

    async def test_trigger_compression_noop_below_threshold(self) -> None:
        ctx = ResearchContext()
        ctx.add_entry(
            FindingEntry(step=1, tool_name="t", query="q", raw_content="short")
        )
        await ctx.trigger_compression(step=5)
        self.assertFalse(ctx.entries[0].compact_content)
        self.assertEqual(ctx.entries[0].raw_content, "short")

    def test_build_prompt_context_three_tiers(self) -> None:
        ctx = ResearchContext()
        ctx.update_summary("全局摘要内容")
        # Old compressed entry
        ctx.add_entry(
            FindingEntry(
                step=1,
                tool_name="web_search",
                query="q",
                raw_content="",
                compact_content="关键内容：旧摘要…\n来源：https://old.com",
            )
        )
        # Recent raw entry
        ctx.add_entry(
            FindingEntry(step=3, tool_name="academic_search", query="q", raw_content="最新结果")
        )
        ctx.current_step = 3

        output = ctx.build_prompt_context()
        self.assertIn("## 研究总结", output)
        self.assertIn("全局摘要内容", output)
        self.assertIn("## 早期发现（压缩）", output)
        self.assertIn("旧摘要", output)
        self.assertIn("## 最近发现（完整）", output)
        self.assertIn("最新结果", output)

    def test_build_prompt_context_truncates(self) -> None:
        ctx = ResearchContext()
        ctx.update_summary("S" * 5000)
        output = ctx.build_prompt_context(max_summary_tokens=100)
        summary_section = output.split("## 研究总结\n")[1].split("\n\n")[0]
        self.assertLessEqual(len(summary_section), 102)  # 100 + "…"

    def test_context_round_trip(self) -> None:
        ctx = ResearchContext()
        ctx.update_summary("test summary")
        ctx.add_entry(
            FindingEntry(step=1, tool_name="t", query="q", raw_content="data")
        )
        data = ctx.to_dict()
        restored = ResearchContext.from_dict(data)
        self.assertEqual(restored.summary, "test summary")
        self.assertEqual(len(restored.entries), 1)
        self.assertEqual(restored.entries[0].raw_content, "data")
        self.assertEqual(restored.current_step, 1)

    def test_empty_context(self) -> None:
        ctx = ResearchContext()
        output = ctx.build_prompt_context()
        self.assertIn("## 研究总结", output)
        self.assertIn("(暂无总结)", output)
        data = ctx.to_dict()
        restored = ResearchContext.from_dict(data)
        self.assertEqual(restored.entries, [])

    def test_build_final_context_includes_all_entries(self) -> None:
        ctx = ResearchContext()
        ctx.update_summary("研究总结内容")
        ctx.add_entry(FindingEntry(step=1, tool_name="web_search", query="q",
                                   raw_content="", compact_content="压缩后的第一步",
                                   source_urls=["https://a.com"]))
        ctx.add_entry(FindingEntry(step=3, tool_name="academic_search", query="q",
                                   raw_content="第三步的完整内容"))
        output = ctx.build_final_context()
        self.assertIn("研究总结内容", output)
        self.assertIn("压缩后的第一步", output)
        self.assertIn("第三步的完整内容", output)
        self.assertIn("https://a.com", output)

    def test_build_final_context_respects_max_chars(self) -> None:
        ctx = ResearchContext()
        ctx.add_entry(FindingEntry(step=1, tool_name="t", query="q",
                                   raw_content="A" * 20000))
        output = ctx.build_final_context(max_chars=500)
        self.assertLessEqual(len(output), 500)

    def test_build_final_context_sorted_by_step(self) -> None:
        ctx = ResearchContext()
        ctx.add_entry(FindingEntry(step=3, tool_name="t", query="q", raw_content="step3"))
        ctx.add_entry(FindingEntry(step=1, tool_name="t", query="q", raw_content="step1"))
        output = ctx.build_final_context()
        self.assertLess(output.find("step1"), output.find("step3"))

    async def test_trigger_compression_priority_order(self) -> None:
        """Weak entries compressed before strong when over budget."""
        ctx = ResearchContext()
        ctx.add_entry(FindingEntry(step=1, tool_name="t", query="q",
                                   raw_content="W" * 5000, evidence_strength="weak"))
        ctx.add_entry(FindingEntry(step=1, tool_name="t", query="q",
                                   raw_content="S" * 5000, evidence_strength="strong"))
        await ctx.trigger_compression(step=5, token_budget=6000, query="test")
        self.assertEqual(ctx.entries[0].raw_content, "")
        self.assertTrue(len(ctx.entries[1].raw_content) > 0)


class ExtractUrlsTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_urls(self) -> None:
        text = "Visit https://a.com/page and http://b.org/doc. Also https://a.com/page again."
        urls = _extract_urls(text)
        self.assertEqual(urls, ["https://a.com/page", "http://b.org/doc"])

    def test_extract_urls_empty(self) -> None:
        self.assertEqual(_extract_urls("no urls here"), [])

    def test_context_to_dict_includes_version(self) -> None:
        from agent.capabilities.context_manager import CONTEXT_VERSION
        ctx = ResearchContext()
        data = ctx.to_dict()
        self.assertEqual(data["_version"], CONTEXT_VERSION)

    def test_finding_entry_to_dict_includes_version(self) -> None:
        from agent.capabilities.context_manager import FINDING_ENTRY_VERSION
        entry = FindingEntry(step=1, tool_name="test", query="q", raw_content="content")
        data = entry.to_dict()
        self.assertEqual(data["_version"], FINDING_ENTRY_VERSION)

    async def test_trigger_compression_with_budget(self) -> None:
        ctx = ResearchContext()
        # Add entries at steps 1 and 2
        ctx.add_entry(FindingEntry(step=1, tool_name="t", query="q", raw_content="A" * 5000))
        ctx.add_entry(FindingEntry(step=2, tool_name="t", query="q", raw_content="B" * 5000))
        # Budget of 200 should trigger aggressive compression (step age >= 1)
        await ctx.trigger_compression(step=3, token_budget=200)
        # Both should be compressed (ages 2 and 1 respectively)
        self.assertTrue(ctx.entries[0].compact_content)
        self.assertEqual(ctx.entries[0].raw_content, "")
        self.assertTrue(ctx.entries[1].compact_content)

    async def test_trigger_compression_budget_zero_no_change(self) -> None:
        ctx = ResearchContext()
        ctx.add_entry(FindingEntry(step=1, tool_name="t", query="q", raw_content="short"))
        await ctx.trigger_compression(step=5, token_budget=0)
        self.assertFalse(ctx.entries[0].compact_content)
        self.assertEqual(ctx.entries[0].raw_content, "short")

    def test_compact_entry_custom_snippet_len(self) -> None:
        ctx = ResearchContext()
        entry = FindingEntry(step=1, tool_name="t", query="q", raw_content="A" * 500)
        ctx.add_entry(entry)
        ctx._compact_entry(entry, snippet_len=100)
        # Snippet should start with first 100 chars (fallback, no structured lines)
        self.assertTrue(entry.compact_content.startswith("A" * 100))

    async def test_compression_already_compacted_skipped(self) -> None:
        """Entry with existing compact_content should not be re-compacted."""
        ctx = ResearchContext()
        entry = FindingEntry(
            step=1, tool_name="t", query="q",
            raw_content="A" * 9000,
            compact_content="Already compacted",
        )
        ctx.add_entry(entry)
        await ctx.trigger_compression(step=5)
        # compact_content should remain unchanged
        self.assertEqual(entry.compact_content, "Already compacted")

    def test_compact_entry_extracts_structured_lines(self) -> None:
        """Smart compaction should extract headings, list items, and data lines."""
        ctx = ResearchContext()
        raw = (
            "Some navigation text\n"
            "Cookie policy notice\n\n"
            "# GNSS Accuracy Study\n"
            "- Open sky accuracy: 3.2m\n"
            "- Urban canyon: 15.7m\n"
            "More filler text that is not important\n"
            "**Key conclusion: multipath degrades accuracy by 5x**\n"
        )
        entry = FindingEntry(step=1, tool_name="web_search", query="GNSS",
                             raw_content=raw, source_urls=["https://example.com"])
        ctx.add_entry(entry)
        ctx._compact_entry(entry)
        # Should contain the structured lines, not the filler
        self.assertIn("GNSS Accuracy Study", entry.compact_content)
        self.assertIn("3.2m", entry.compact_content)
        self.assertIn("multipath", entry.compact_content)
        self.assertNotIn("Cookie policy", entry.compact_content)
        self.assertEqual(entry.raw_content, "")

    def test_compact_entry_fallback_to_prefix(self) -> None:
        """When no structured lines found, fall back to prefix truncation."""
        ctx = ResearchContext()
        entry = FindingEntry(step=1, tool_name="t", query="q",
                             raw_content="plain text without any structure " * 20)
        ctx.add_entry(entry)
        ctx._compact_entry(entry, snippet_len=100)
        self.assertTrue(len(entry.compact_content) > 0)
        self.assertEqual(entry.raw_content, "")

    def test_build_prompt_context_large_summary_truncated(self) -> None:
        ctx = ResearchContext()
        ctx.update_summary("S" * 10000)
        output = ctx.build_prompt_context(max_summary_tokens=500)
        summary_section = output.split("## 研究总结\n")[1].split("\n\n")[0]
        self.assertLessEqual(len(summary_section), 502)


if __name__ == "__main__":
    unittest.main()
