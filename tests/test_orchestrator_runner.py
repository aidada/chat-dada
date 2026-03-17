from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from orchestrator.runner import _handle_generic


class GenericRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_generic_emits_download_event_and_summary_for_renderer(self) -> None:
        emitted: list[str] = []

        async def on_step(message: str) -> None:
            emitted.append(message)

        async def fake_execute_plan(steps, context, on_step_callback):
            return {
                "step_1": {
                    "status": "ok",
                    "result": "## 研究结论\n- NLOS 多普勒频移主要受信号实际到达方向影响\n- 速度先验可以缩小候选路径搜索空间",
                },
                "step_2": "",
                "step_3": {
                    "status": "ok",
                    "result": "Markdown file saved: outputs/gnss_report.md",
                    "files": ["outputs/gnss_report.md"],
                },
            }

        plan = {
            "steps": [
                {"id": 1, "type": "agent", "name": "deep_research", "depends_on": []},
                {"id": 2, "type": "agent", "name": "doc_analyst", "depends_on": []},
                {"id": 3, "type": "renderer", "name": "markdown_render", "depends_on": [1, 2]},
            ],
            "context": {
                "title": "GNSS NLOS 研究报告",
                "task": "帮我调研 GNSS NLOS 方向估计",
            },
        }

        with patch("orchestrator.scheduler.execute_plan", side_effect=fake_execute_plan):
            result = await _handle_generic("帮我调研 GNSS NLOS 方向估计", plan, on_step)

        file_events = [json.loads(message) for message in emitted if message.startswith("{")]
        self.assertEqual(
            file_events,
            [{"type": "file", "url": "/download/gnss_report.md", "name": "gnss_report.md"}],
        )
        self.assertIn("《GNSS NLOS 研究报告》已生成", result)
        self.assertIn("内容摘要", result)
        self.assertIn("研究结论", result)

    async def test_handle_generic_summarizes_structured_result_blocks_for_renderer(self) -> None:
        emitted: list[str] = []

        async def on_step(message: str) -> None:
            emitted.append(message)

        async def fake_execute_plan(steps, context, on_step_callback):
            return {
                "step_1": {
                    "status": "ok",
                    "result": [
                        {"id": "rs_1", "summary": [], "type": "reasoning"},
                        {"type": "text", "text": "## 直接结论\n- 多普勒可用于约束 NLOS 到达方向"},
                    ],
                },
                "step_3": {
                    "status": "ok",
                    "result": "Markdown file saved: outputs/gnss_report.md",
                    "files": ["outputs/gnss_report.md"],
                },
            }

        plan = {
            "steps": [
                {"id": 1, "type": "agent", "name": "deep_research", "depends_on": []},
                {"id": 3, "type": "renderer", "name": "markdown_render", "depends_on": [1]},
            ],
            "context": {
                "title": "GNSS NLOS 研究报告",
                "task": "帮我调研 GNSS NLOS 方向估计",
            },
        }

        with patch("orchestrator.scheduler.execute_plan", side_effect=fake_execute_plan):
            result = await _handle_generic("帮我调研 GNSS NLOS 方向估计", plan, on_step)

        self.assertIn("## 直接结论", result)
        self.assertNotIn("'id': 'rs_1'", result)

    async def test_handle_generic_returns_failure_summary_when_upstream_step_fails(self) -> None:
        emitted: list[str] = []

        async def on_step(message: str) -> None:
            emitted.append(message)

        async def fake_execute_plan(steps, context, on_step_callback):
            return {
                "step_1_error": "BrowserSession.__init__() got an unexpected keyword argument 'config'",
                "step_2": "",
                "step_3_error": "Blocked by failed dependency: deep_research: BrowserSession.__init__() got an unexpected keyword argument 'config'",
            }

        plan = {
            "steps": [
                {"id": 1, "type": "agent", "name": "deep_research", "depends_on": []},
                {"id": 2, "type": "agent", "name": "doc_analyst", "depends_on": []},
                {"id": 3, "type": "renderer", "name": "markdown_render", "depends_on": [1, 2]},
            ],
            "context": {
                "title": "GNSS NLOS 研究报告",
                "task": "帮我调研 GNSS NLOS 方向估计",
            },
        }

        with patch("orchestrator.scheduler.execute_plan", side_effect=fake_execute_plan):
            result = await _handle_generic("帮我调研 GNSS NLOS 方向估计", plan, on_step)

        self.assertIn("任务未能完成", result)
        self.assertIn("deep_research", result)
        self.assertIn("markdown_render", result)
        self.assertEqual([message for message in emitted if message.startswith("{")], [])
