from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.scheduler import execute_plan


class SchedulerWriterInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_plan_builds_writer_input_from_dependencies(self) -> None:
        received: dict[str, dict] = {}

        async def fake_deep_research(input_data):
            self.assertEqual(input_data, "AI agents")
            return {"status": "ok", "result": "search findings"}

        async def fake_doc_analyst(input_data):
            self.assertEqual(input_data, ["/tmp/report.pdf"])
            return "doc analysis"

        async def fake_writer(input_data):
            received["writer_input"] = input_data
            return {"status": "ok", "result": "draft"}

        def fake_resolve_fn(name: str):
            return {
                "deep_research": fake_deep_research,
                "doc_analyst": fake_doc_analyst,
                "writer": fake_writer,
            }[name]

        steps = [
            {"id": 1, "type": "agent", "name": "deep_research", "input_key": "search_query"},
            {"id": 2, "type": "agent", "name": "doc_analyst", "input_key": "file_paths"},
            {"id": 3, "type": "agent", "name": "writer", "input_key": "writer_input", "depends_on": [1, 2]},
        ]
        context = {
            "search_query": "AI agents",
            "file_paths": ["/tmp/report.pdf"],
            "storyline": "背景介绍\n核心发现\n结论建议",
            "author": "Alice",
        }

        with patch("orchestrator.scheduler.resolve_fn", side_effect=fake_resolve_fn):
            result_ctx = await execute_plan(steps, context)

        self.assertEqual(
            received["writer_input"],
            {
                "storyline": "背景介绍\n核心发现\n结论建议",
                "search_findings": "search findings",
                "doc_analysis": "doc analysis",
                "author": "Alice",
            },
        )
        self.assertEqual(result_ctx["step_3"], {"status": "ok", "result": "draft"})

    async def test_execute_plan_builds_markdown_render_input_from_dependencies(self) -> None:
        received: dict[str, dict] = {}

        async def fake_deep_research(input_data):
            self.assertEqual(input_data, "GNSS NLOS")
            return {
                "status": "ok",
                "result": "## 核心结论\n- 多普勒频移可用于约束 NLOS 到达方向\n- 速度先验能缩小候选路径空间",
            }

        async def fake_doc_analyst(input_data):
            self.assertEqual(input_data, [])
            return ""

        def fake_markdown_render(input_data):
            received["render_input"] = input_data
            return {
                "status": "ok",
                "result": "Markdown file saved: outputs/gnss_report.md",
                "files": ["outputs/gnss_report.md"],
            }

        def fake_resolve_fn(name: str):
            return {
                "deep_research": fake_deep_research,
                "doc_analyst": fake_doc_analyst,
                "markdown_render": fake_markdown_render,
            }[name]

        steps = [
            {"id": 1, "type": "agent", "name": "deep_research", "input_key": "search_query"},
            {"id": 2, "type": "agent", "name": "doc_analyst", "input_key": "file_paths"},
            {"id": 3, "type": "renderer", "name": "markdown_render", "input_key": "render_input", "depends_on": [1, 2]},
        ]
        context = {
            "search_query": "GNSS NLOS",
            "file_paths": [],
            "title": "GNSS 研究报告",
            "task": "帮我调研 GNSS NLOS 方向估计",
        }

        with patch("orchestrator.scheduler.resolve_fn", side_effect=fake_resolve_fn):
            result_ctx = await execute_plan(steps, context)

        self.assertEqual(received["render_input"]["title"], "GNSS 研究报告")
        self.assertIn("核心结论", received["render_input"]["content"])
        self.assertTrue(received["render_input"]["output_path"].startswith("outputs/"))
        self.assertTrue(received["render_input"]["output_path"].endswith(".md"))
        self.assertEqual(
            result_ctx["step_3"],
            {
                "status": "ok",
                "result": "Markdown file saved: outputs/gnss_report.md",
                "files": ["outputs/gnss_report.md"],
            },
        )

    async def test_execute_plan_builds_markdown_render_input_from_structured_blocks(self) -> None:
        received: dict[str, dict] = {}

        async def fake_deep_research(input_data):
            return {
                "status": "ok",
                "result": [
                    {"id": "rs_1", "summary": [], "type": "reasoning"},
                    {"type": "text", "text": "## 核心结论\n- 多普勒可提供方向约束"},
                ],
            }

        def fake_markdown_render(input_data):
            received["render_input"] = input_data
            return {"status": "ok", "files": ["outputs/report.md"]}

        def fake_resolve_fn(name: str):
            return {
                "deep_research": fake_deep_research,
                "markdown_render": fake_markdown_render,
            }[name]

        steps = [
            {"id": 1, "type": "agent", "name": "deep_research", "input_key": "search_query"},
            {"id": 2, "type": "renderer", "name": "markdown_render", "input_key": "render_input", "depends_on": [1]},
        ]
        context = {"search_query": "GNSS NLOS", "title": "GNSS 研究报告"}

        with patch("orchestrator.scheduler.resolve_fn", side_effect=fake_resolve_fn):
            await execute_plan(steps, context)

        self.assertEqual(received["render_input"]["content"], "## 核心结论\n- 多普勒可提供方向约束")

    async def test_execute_plan_blocks_dependent_steps_after_failure(self) -> None:
        called: list[str] = []

        async def fake_deep_research(input_data):
            raise RuntimeError("browser tool failed")

        def fake_markdown_render(input_data):
            called.append("markdown_render")
            return {"status": "ok", "files": ["outputs/report.md"]}

        def fake_resolve_fn(name: str):
            return {
                "deep_research": fake_deep_research,
                "markdown_render": fake_markdown_render,
            }[name]

        steps = [
            {"id": 1, "type": "agent", "name": "deep_research", "input_key": "search_query"},
            {"id": 2, "type": "renderer", "name": "markdown_render", "input_key": "render_input", "depends_on": [1]},
        ]
        context = {"search_query": "GNSS NLOS", "title": "GNSS 研究报告"}

        with patch("orchestrator.scheduler.resolve_fn", side_effect=fake_resolve_fn):
            result_ctx = await execute_plan(steps, context)

        self.assertEqual(called, [])
        self.assertEqual(result_ctx["step_1_error"], "browser tool failed")
        self.assertIn("Blocked by failed dependency", result_ctx["step_2_error"])
