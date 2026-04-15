"""Tests for domain.conversations.context module."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from domain.conversations.context import (
    ConversationContext,
    ConversationContextBuilder,
    _Round,
    _format_rounds_raw,
    _needs_retrieval,
    _truncate,
    _estimate_tokens,
)


class TruncateTest(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(_truncate("hello", 400), "hello")

    def test_long_text_truncated(self):
        long = "a" * 500
        result = _truncate(long, 400)
        self.assertEqual(len(result), 400)
        self.assertTrue(result.endswith("…"))


class EstimateTokensTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_estimate_tokens(""), 0)

    def test_rough_estimate(self):
        # 150 chars -> ~100 tokens
        self.assertEqual(_estimate_tokens("a" * 150), 100)


class NeedsRetrievalTest(unittest.TestCase):
    def test_short_message_triggers(self):
        self.assertTrue(_needs_retrieval("继续"))
        self.assertTrue(_needs_retrieval("那个呢"))

    def test_referential_marker_triggers(self):
        self.assertTrue(_needs_retrieval("结合之前的讨论，请总结一下"))
        self.assertTrue(_needs_retrieval("请对比一下两种方案的优劣"))

    def test_self_contained_skips(self):
        self.assertFalse(_needs_retrieval("今天北京的天气怎么样？请告诉我详细的天气预报"))

    def test_english_markers(self):
        self.assertTrue(_needs_retrieval("Based on that previous discussion, what do you think?"))
        self.assertTrue(_needs_retrieval("Can you elaborate on that earlier point about APIs?"))


class FormatRoundsRawTest(unittest.TestCase):
    def test_single_round(self):
        rounds = [_Round(task_id="t1", user_query="你好", assistant_reply="你好！")]
        result = _format_rounds_raw(rounds)
        self.assertIn("Q: 你好", result)
        self.assertIn("A: 你好！", result)

    def test_multiple_rounds(self):
        rounds = [
            _Round(task_id="t1", user_query="问题1", assistant_reply="回答1"),
            _Round(task_id="t2", user_query="问题2", assistant_reply="回答2"),
        ]
        result = _format_rounds_raw(rounds)
        self.assertIn("Q: 问题1", result)
        self.assertIn("A: 回答2", result)

    def test_long_content_truncated(self):
        long_query = "x" * 500
        rounds = [_Round(task_id="t1", user_query=long_query, assistant_reply="ok")]
        result = _format_rounds_raw(rounds)
        # Should be truncated to 400 chars
        self.assertTrue(len(result.split("\n\n")[0]) <= 410)  # Q: prefix + truncated


class BuildRawTest(unittest.IsolatedAsyncioTestCase):
    def _make_builder(self):
        return ConversationContextBuilder()

    def test_build_raw_formats_correctly(self):
        builder = self._make_builder()
        rounds = [
            _Round(task_id="t1", user_query="什么是GNSS", assistant_reply="GNSS是全球导航卫星系统"),
            _Round(task_id="t2", user_query="精度如何", assistant_reply="RTK可达厘米级"),
        ]
        ctx = builder._build_raw(rounds)
        self.assertEqual(ctx.strategy, "raw")
        self.assertEqual(ctx.round_count, 2)
        self.assertIn("[对话历史]", ctx.text)
        self.assertIn("GNSS", ctx.text)

    def test_build_raw_truncates_to_budget(self):
        builder = self._make_builder()
        # Create rounds with very long content to exceed budget
        rounds = [
            _Round(task_id=f"t{i}", user_query="q" * 400, assistant_reply="a" * 400)
            for i in range(20)
        ]
        ctx = builder._build_raw(rounds)
        tokens = _estimate_tokens(ctx.text)
        self.assertLessEqual(tokens, 8100)  # slightly over is ok due to estimation


class BuildContextIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_conversation_id(self):
        builder = ConversationContextBuilder()
        ctx = await builder.build("", "hello")
        self.assertEqual(ctx.text, "")
        self.assertEqual(ctx.strategy, "none")

    @patch("domain.conversations.context._get_conversation_rounds", new_callable=AsyncMock)
    async def test_no_completed_rounds(self, mock_rounds):
        builder = ConversationContextBuilder()
        mock_rounds.return_value = []
        ctx = await builder.build("conv1", "hello")
        self.assertEqual(ctx.round_count, 0)
        self.assertEqual(ctx.strategy, "none")

    @patch("domain.conversations.context._get_conversation_rounds", new_callable=AsyncMock)
    async def test_raw_strategy_for_few_rounds(self, mock_rounds):
        """≤5 rounds should use raw strategy."""
        mock_rounds.return_value = [
            _Round(task_id=f"t{i}", user_query=f"问题{i}", assistant_reply=f"回答{i}")
            for i in range(3)
        ]
        builder = ConversationContextBuilder()
        ctx = await builder.build("conv1", "新问题")
        self.assertEqual(ctx.strategy, "raw")
        self.assertEqual(ctx.round_count, 3)
        self.assertIn("问题0", ctx.text)
        self.assertIn("回答2", ctx.text)

    @patch("domain.conversations.context._get_conversation_rounds", new_callable=AsyncMock)
    async def test_summary_strategy_for_many_rounds(self, mock_rounds):
        """6-20 rounds should use summary strategy."""
        mock_rounds.return_value = [
            _Round(task_id=f"t{i}", user_query=f"问题{i}", assistant_reply=f"回答{i}")
            for i in range(8)
        ]
        builder = ConversationContextBuilder()
        with patch.object(builder, "_get_or_update_summary", AsyncMock(return_value="这是一段摘要")):
            ctx = await builder.build("conv1", "新问题")
            self.assertEqual(ctx.strategy, "summary")
            self.assertEqual(ctx.round_count, 8)
            self.assertIn("[对话历史摘要]", ctx.text)
            self.assertIn("[最近对话]", ctx.text)


class ConversationContextDataclassTest(unittest.TestCase):
    def test_defaults(self):
        ctx = ConversationContext()
        self.assertEqual(ctx.text, "")
        self.assertEqual(ctx.round_count, 0)
        self.assertEqual(ctx.strategy, "none")

    def test_custom_values(self):
        ctx = ConversationContext(text="hello", round_count=5, strategy="raw")
        self.assertEqual(ctx.text, "hello")
        self.assertEqual(ctx.round_count, 5)
        self.assertEqual(ctx.strategy, "raw")


if __name__ == "__main__":
    unittest.main()
