from __future__ import annotations

import unittest
from unittest.mock import patch

import browser_use
from langchain_core.messages import AIMessage, ToolMessage

from agents import deep_research


class _FakeBrowserProfile:
    def __init__(self, headless: bool = False) -> None:
        self.headless = headless


class _FakeBrowserSession:
    def __init__(self, browser_profile=None, **kwargs) -> None:
        self.browser_profile = browser_profile
        self.kwargs = kwargs


class _FakeAgentResult:
    def final_result(self) -> str:
        return "Browser task done."


class _FakeBrowserAgent:
    def __init__(self, task, llm, browser, max_actions_per_step) -> None:
        self.task = task
        self.llm = llm
        self.browser = browser
        self.max_actions_per_step = max_actions_per_step

    async def run(self, max_steps: int = 10):
        return _FakeAgentResult()


class _FakeBoundLLM:
    async def ainvoke(self, messages):
        return "ok"


class _FakeLLM:
    def bind_tools(self, tools):
        return _FakeBoundLLM()


class DeepResearchTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_report_profile_auto_selects_academic_paper_guidance(self) -> None:
        profile = deep_research._resolve_report_profile(
            "请做文献综述，并说明这篇论文后续应该怎么写 introduction 和 experiment",
        )

        self.assertEqual(profile, deep_research.ACADEMIC_PAPER_GUIDANCE_PROFILE)

    def test_resolve_report_profile_honors_explicit_override(self) -> None:
        profile = deep_research._resolve_report_profile(
            "请做市场调研",
            requested_profile="academic",
        )

        self.assertEqual(profile, deep_research.ACADEMIC_PAPER_GUIDANCE_PROFILE)

    def test_build_research_messages_include_academic_writing_sections(self) -> None:
        messages = deep_research._build_research_messages(
            "请帮我做论文引言相关的文献综述",
            "### note\nexisting",
            deep_research.ACADEMIC_PAPER_GUIDANCE_PROFILE,
        )

        self.assertEqual(len(messages), 2)
        self.assertIn("科研论文写作导向", messages[0].content)
        self.assertIn("## 对后续论文写作的明确建议", messages[1].content)
        self.assertIn("## 建议补充的实验与材料", messages[1].content)

    async def test_browser_navigate_uses_supported_browser_use_imports(self) -> None:
        with (
            patch.object(browser_use, "Agent", _FakeBrowserAgent),
            patch.object(browser_use, "BrowserSession", _FakeBrowserSession),
            patch.object(browser_use, "BrowserProfile", _FakeBrowserProfile),
            patch("agents.deep_research.get_browser_use_llm", return_value="mock-llm") as mocked_get_llm,
        ):
            result = await deep_research.browser_navigate.ainvoke(
                {"task_description": "Open example.com"}
            )

        mocked_get_llm.assert_called_once_with("deep_research")
        self.assertEqual(result, "Browser task done.")

    async def test_research_planner_uses_deep_research_role(self) -> None:
        state = {"messages": [], "query": "GNSS", "step_count": 0, "findings": ""}

        with patch("agents.deep_research.get_llm", return_value=_FakeLLM()) as mocked_get_llm:
            result = await deep_research.research_planner(state)

        mocked_get_llm.assert_called_once_with("deep_research")
        self.assertEqual(result["step_count"], 1)
        self.assertEqual(result["messages"], ["ok"])

    async def test_research_planner_builds_compact_prompt_from_findings(self) -> None:
        captured: dict[str, object] = {}

        class _InspectBoundLLM:
            async def ainvoke(self, messages):
                captured["messages"] = messages
                return AIMessage(content="final answer")

        class _InspectLLM:
            def bind_tools(self, tools):
                return _InspectBoundLLM()

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "call_1", "name": "brave_search", "args": {"query": "GNSS"}}],
                ),
                ToolMessage(content="A" * 1200, tool_call_id="call_1", name="brave_search"),
            ],
            "query": "GNSS NLOS",
            "step_count": 1,
            "findings": "### existing\nold note",
        }

        with patch("agents.deep_research.get_llm", return_value=_InspectLLM()):
            result = await deep_research.research_planner(state)

        prompt_messages = captured["messages"]
        self.assertEqual(len(prompt_messages), 2)
        self.assertIn("当前研究笔记（已压缩）", prompt_messages[1].content)
        self.assertIn("old note", prompt_messages[1].content)
        self.assertIn("### brave_search", result["findings"])
        self.assertLess(len(result["findings"]), 6001)

    async def test_research_finish_extracts_text_from_responses_blocks(self) -> None:
        state = {
            "messages": [
                AIMessage(
                    content=[
                        {"id": "rs_1", "summary": [], "type": "reasoning"},
                        {"type": "text", "text": "**直接结论**\n\n可以，但依赖额外几何约束。"},
                    ]
                )
            ],
            "query": "GNSS NLOS",
            "step_count": 2,
            "findings": "",
        }

        result = deep_research.research_finish(state)

        self.assertEqual(result["findings"], "## 直接结论\n\n可以，但依赖额外几何约束。")

    async def test_run_rewrites_final_report_with_markdown_headings(self) -> None:
        class _FakeGraph:
            async def ainvoke(self, state):
                return {"findings": "raw notes"}

        class _RewriteLLM:
            async def ainvoke(self, messages):
                return AIMessage(
                    content=[
                        {"type": "reasoning", "summary": []},
                        {"type": "text", "text": "**直接结论**\n\n结论更聚焦。"},
                    ]
                )

        with (
            patch("agents.deep_research.build_research_graph", return_value=_FakeGraph()),
            patch("agents.deep_research.get_llm", return_value=_RewriteLLM()),
        ):
            result = await deep_research.run("GNSS NLOS")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result"], "## 直接结论\n\n结论更聚焦。")

    async def test_run_accepts_nested_search_query_dict_with_report_profile(self) -> None:
        captured: dict[str, object] = {}

        class _FakeGraph:
            async def ainvoke(self, state):
                captured["state"] = state
                return {"findings": "raw notes"}

        class _RewriteLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="## 文献综述正文\n\n聚焦论文写作建议。")

        with (
            patch("agents.deep_research.build_research_graph", return_value=_FakeGraph()),
            patch("agents.deep_research.get_llm", return_value=_RewriteLLM()),
        ):
            result = await deep_research.run(
                {
                    "search_query": {
                        "query": "请做文献综述并指导后续论文写作",
                        "report_profile": "academic_paper_guidance",
                    }
                }
            )

        self.assertEqual(captured["state"]["query"], "请做文献综述并指导后续论文写作")
        self.assertEqual(
            captured["state"]["report_profile"],
            deep_research.ACADEMIC_PAPER_GUIDANCE_PROFILE,
        )
        self.assertEqual(result["result"], "## 文献综述正文\n\n聚焦论文写作建议。")

    async def test_rewrite_final_report_uses_academic_profile_instructions(self) -> None:
        captured: dict[str, object] = {}

        class _InspectRewriteLLM:
            async def ainvoke(self, messages):
                captured["messages"] = messages
                return AIMessage(content="## 文献综述正文\n\nAcademic draft.")

        with patch("agents.deep_research.get_llm", return_value=_InspectRewriteLLM()):
            result = await deep_research._rewrite_final_report(
                "请帮我做后续论文写作指导",
                "raw notes",
                deep_research.ACADEMIC_PAPER_GUIDANCE_PROFILE,
            )

        prompt_messages = captured["messages"]
        self.assertIn("对后续论文写作的明确建议", prompt_messages[0].content)
        self.assertIn("当前输出模板：academic_paper_guidance", prompt_messages[1].content)
        self.assertEqual(result, "## 文献综述正文\n\nAcademic draft.")
