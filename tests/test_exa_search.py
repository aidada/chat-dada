"""Tests for tools/exa_search.py"""
from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock


class ExaSearchTests(unittest.IsolatedAsyncioTestCase):

    async def test_run_string_input(self):
        """Simple string query should work."""
        mock_result = MagicMock()
        mock_result.title = "Test Paper"
        mock_result.url = "https://example.com/paper"
        mock_result.text = None
        mock_result.published_date = "2024-01-01"
        mock_result.author = "Author Name"
        mock_result.highlights = ["relevant highlight"]
        mock_result.summary = "Paper summary"
        mock_result.score = 0.95

        mock_response = MagicMock()
        mock_response.results = [mock_result]
        mock_response.output = None
        mock_response.resolved_search_type = "deep"
        mock_response.search_time = 120
        mock_response.cost_dollars = None

        mock_exa_instance = MagicMock()
        mock_exa_instance.search.return_value = mock_response

        with patch("tools.exa_search.HAS_EXA", True), \
             patch("tools.exa_search.Exa", return_value=mock_exa_instance), \
             patch.dict("os.environ", {"EXA_API_KEY": "test-key"}):
            from agent.tools.exa_search import run
            result = await run("GNSS multipath")

        self.assertEqual(result["status"], "ok")
        self.assertIn("Test Paper", result["result"])
        self.assertIn("https://example.com/paper", result["result"])
        self.assertIn("摘要", result["result"])
        mock_exa_instance.search.assert_called_once()

    async def test_run_dict_input_with_params(self):
        """Dict input with advanced params should pass them through."""
        mock_response = MagicMock()
        mock_response.results = []

        mock_exa_instance = MagicMock()
        mock_exa_instance.search.return_value = mock_response

        with patch("tools.exa_search.HAS_EXA", True), \
             patch("tools.exa_search.Exa", return_value=mock_exa_instance), \
             patch.dict("os.environ", {"EXA_API_KEY": "test-key"}):
            from agent.tools.exa_search import run
            result = await run({
                "query": "GNSS",
                "type": "deep",
                "category": "research paper",
                "num_results": 5,
            })

        self.assertEqual(result["status"], "ok")
        self.assertIn("no results", result["result"].lower())
        call_kwargs = mock_exa_instance.search.call_args
        self.assertEqual(call_kwargs[1]["type"], "deep")
        self.assertEqual(call_kwargs[1]["category"], "research paper")
        self.assertEqual(call_kwargs[1]["num_results"], 5)
        self.assertIn("summary", call_kwargs[1]["contents"])
        self.assertIn("highlights", call_kwargs[1]["contents"])

    async def test_run_no_library(self):
        """Missing exa_py should return graceful fallback."""
        with patch("tools.exa_search.HAS_EXA", False):
            from agent.tools.exa_search import run
            result = await run("test query")

        self.assertEqual(result["status"], "ok")
        self.assertIn("unavailable", result["result"].lower())

    async def test_run_no_api_key(self):
        """Missing API key should return graceful fallback."""
        with patch("tools.exa_search.HAS_EXA", True), \
             patch.dict("os.environ", {}, clear=True):
            from agent.tools.exa_search import run
            result = await run("test query")

        self.assertEqual(result["status"], "ok")
        self.assertIn("EXA_API_KEY", result["result"])

    async def test_run_api_error(self):
        """API exception should return error status."""
        mock_exa_instance = MagicMock()
        mock_exa_instance.search.side_effect = ConnectionError("timeout")

        with patch("tools.exa_search.HAS_EXA", True), \
             patch("tools.exa_search.Exa", return_value=mock_exa_instance), \
             patch.dict("os.environ", {"EXA_API_KEY": "test-key"}):
            from agent.tools.exa_search import run
            result = await run("test query")

        self.assertEqual(result["status"], "error")
        self.assertIn("timeout", result["result"])

    async def test_run_invalid_category_ignored(self):
        """Invalid category should be silently dropped."""
        mock_response = MagicMock()
        mock_response.results = []
        mock_exa_instance = MagicMock()
        mock_exa_instance.search.return_value = mock_response

        with patch("tools.exa_search.HAS_EXA", True), \
             patch("tools.exa_search.Exa", return_value=mock_exa_instance), \
             patch.dict("os.environ", {"EXA_API_KEY": "test-key"}):
            from agent.tools.exa_search import run
            await run({"query": "test", "category": "invalid_cat"})

        call_kwargs = mock_exa_instance.search.call_args[1]
        self.assertNotIn("category", call_kwargs)

    async def test_run_full_text_mode_uses_get_contents_and_returns_json(self):
        """全文模式应二次调用 get_contents，并默认输出结构化 JSON。"""
        search_result = MagicMock()
        search_result.title = "Paper A"
        search_result.url = "https://example.com/paper-a"
        search_result.summary = "short summary"
        search_result.highlights = ["h1"]
        search_result.published_date = "2024-02-02"
        search_result.author = "Author A"
        search_result.score = 0.9

        contents_result = MagicMock()
        contents_result.title = "Paper A"
        contents_result.url = "https://example.com/paper-a"
        contents_result.text = "full text body"
        contents_result.summary = {"finding": "structured"}
        contents_result.highlights = None
        contents_result.published_date = "2024-02-02"
        contents_result.author = "Author A"
        contents_result.score = None

        mock_search_response = MagicMock()
        mock_search_response.results = [search_result]
        mock_search_response.output = None
        mock_search_response.resolved_search_type = "deep"
        mock_search_response.search_time = 200
        mock_search_response.cost_dollars = None

        mock_contents_response = MagicMock()
        mock_contents_response.results = [contents_result]
        mock_contents_response.output = None
        mock_contents_response.resolved_search_type = "deep"
        mock_contents_response.search_time = 200
        mock_contents_response.cost_dollars = None

        mock_exa_instance = MagicMock()
        mock_exa_instance.search.return_value = mock_search_response
        mock_exa_instance.get_contents.return_value = mock_contents_response

        with patch("tools.exa_search.HAS_EXA", True), \
             patch("tools.exa_search.Exa", return_value=mock_exa_instance), \
             patch.dict("os.environ", {"EXA_API_KEY": "test-key"}):
            from agent.tools.exa_search import run
            result = await run({
                "query": "GNSS multipath",
                "result_mode": "full_text",
                "summary_schema": {"type": "object", "properties": {"finding": {"type": "string"}}},
            })

        self.assertEqual(result["status"], "ok")
        self.assertIn('"text": "full text body"', result["result"])
        self.assertIn('"summary"', result["result"])
        mock_exa_instance.search.assert_called_once()
        mock_exa_instance.get_contents.assert_called_once()
