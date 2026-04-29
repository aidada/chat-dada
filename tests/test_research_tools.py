from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from agent.workflows.research import tools as research_tools


class ResearchToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_search_skips_tavily_when_api_key_missing(self) -> None:
        with patch.object(research_tools, "HAS_TAVILY", True), patch.object(
            research_tools,
            "TavilySearchResults",
            side_effect=AssertionError("Tavily should not be constructed without an API key"),
        ), patch.dict(os.environ, {"TAVILY_API_KEY": ""}):
            result = await research_tools.web_search.ainvoke({"query": "AWS S3 pricing"})

        self.assertIn("TAVILY_API_KEY not configured", result)
        self.assertIn("AWS S3 pricing", result)

    async def test_web_search_reports_tavily_initialization_errors(self) -> None:
        with patch.object(research_tools, "HAS_TAVILY", True), patch.object(
            research_tools,
            "TavilySearchResults",
            side_effect=ValueError("bad tavily config"),
        ), patch.dict(os.environ, {"TAVILY_API_KEY": "configured"}, clear=False):
            result = await research_tools.web_search.ainvoke({"query": "AWS S3 pricing"})

        self.assertIn("Tavily search unavailable", result)
        self.assertIn("bad tavily config", result)

    async def test_get_research_tools_omits_web_search_without_tavily_key(self) -> None:
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}, clear=False):
            names = {getattr(tool, "name", "") for tool in research_tools.get_research_tools()}

        self.assertNotIn("web_search", names)
        self.assertIn("exa_deep_search", names)


if __name__ == "__main__":
    unittest.main()
