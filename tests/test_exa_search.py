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
        mock_result.text = "Paper content here"
        mock_result.published_date = "2024-01-01"
        mock_result.author = "Author Name"
        mock_result.highlights = None
        mock_result.summary = None
        mock_result.score = 0.95

        mock_response = MagicMock()
        mock_response.results = [mock_result]

        mock_exa_instance = MagicMock()
        mock_exa_instance.search.return_value = mock_response

        with patch("tools.exa_search.HAS_EXA", True), \
             patch("tools.exa_search.Exa", return_value=mock_exa_instance), \
             patch.dict("os.environ", {"EXA_API_KEY": "test-key"}):
            from tools.exa_search import run
            result = await run("GNSS multipath")

        self.assertEqual(result["status"], "ok")
        self.assertIn("Test Paper", result["result"])
        self.assertIn("https://example.com/paper", result["result"])
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
            from tools.exa_search import run
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

    async def test_run_no_library(self):
        """Missing exa_py should return graceful fallback."""
        with patch("tools.exa_search.HAS_EXA", False):
            from tools.exa_search import run
            result = await run("test query")

        self.assertEqual(result["status"], "ok")
        self.assertIn("unavailable", result["result"].lower())

    async def test_run_no_api_key(self):
        """Missing API key should return graceful fallback."""
        with patch("tools.exa_search.HAS_EXA", True), \
             patch.dict("os.environ", {}, clear=True):
            from tools.exa_search import run
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
            from tools.exa_search import run
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
            from tools.exa_search import run
            await run({"query": "test", "category": "invalid_cat"})

        call_kwargs = mock_exa_instance.search.call_args[1]
        self.assertNotIn("category", call_kwargs)
