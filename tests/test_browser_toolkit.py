from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.capabilities.toolkits.browser_toolkit import browser_navigate_task


class BrowserToolkitTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_task_passes_clean_initial_url(self) -> None:
        captured: dict[str, object] = {}

        class _FakeBrowserProfile:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class _FakeBrowser:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class _FakeAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def run(self, max_steps: int):
                captured["run_max_steps"] = max_steps
                return SimpleNamespace(final_result=lambda: "done")

        task = "访问阿里云 OSS 官方定价页面 https://www.aliyun.com/price/detail/oss，提取所有计费细项。"
        with patch("browser_use.BrowserProfile", _FakeBrowserProfile), patch(
            "browser_use.BrowserSession",
            _FakeBrowser,
        ), patch("browser_use.Agent", _FakeAgent), patch(
            "agent.capabilities.toolkits.browser_toolkit.get_browser_use_llm",
            return_value=SimpleNamespace(provider="test", model="test-model"),
        ):
            result = await browser_navigate_task(task)

        self.assertEqual(result, "done")
        self.assertEqual(
            captured["initial_actions"],
            [{"navigate": {"url": "https://www.aliyun.com/price/detail/oss", "new_tab": False}}],
        )
        self.assertEqual(captured["max_failures"], 2)
        self.assertEqual(captured["llm_timeout"], 45)
        self.assertEqual(captured["step_timeout"], 90)
        self.assertFalse(captured["use_judge"])
        self.assertEqual(captured["run_max_steps"], 6)
        self.assertIn("https://www.aliyun.com/price/detail/oss", str(captured["task"]))
        self.assertNotIn("oss，提取", str(captured["task"]))

    async def test_browser_task_timeout_returns_degraded_result(self) -> None:
        class _FakeBrowserProfile:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class _FakeBrowser:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class _FakeAgent:
            def __init__(self, **kwargs):
                pass

            async def run(self, max_steps: int):
                raise TimeoutError("slow browser task")

        with patch("browser_use.BrowserProfile", _FakeBrowserProfile), patch(
            "browser_use.BrowserSession",
            _FakeBrowser,
        ), patch("browser_use.Agent", _FakeAgent), patch(
            "agent.capabilities.toolkits.browser_toolkit.get_browser_use_llm",
            return_value=SimpleNamespace(provider="test", model="test-model"),
        ):
            result = await browser_navigate_task("打开 https://example.com 并提取内容")

        self.assertIn("Browser task timed out", result)
        self.assertIn("https://example.com", result)


if __name__ == "__main__":
    unittest.main()
