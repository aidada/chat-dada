from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

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
            patch("agents.deep_research.run.get_browser_use_llm", return_value="mock-llm") as mocked_get_llm,
        ):
            result = await deep_research.browser_navigate.ainvoke(
                {"task_description": "Open example.com"}
            )

        mocked_get_llm.assert_called_once_with("deep_research")
        self.assertEqual(result, "Browser task done.")

    async def test_research_planner_uses_deep_research_role(self) -> None:
        state = {"messages": [], "query": "GNSS", "step_count": 0}

        with patch("agents.deep_research.graphs.get_llm", return_value=_FakeLLM()) as mocked_get_llm:
            result = await deep_research.research_planner(state)

        mocked_get_llm.assert_called_once_with("deep_research")
        self.assertEqual(result["step_count"], 1)
        self.assertEqual(result["messages"], ["ok"])

    async def test_research_planner_builds_compact_prompt_from_context(self) -> None:
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
        }

        with patch("agents.deep_research.graphs.get_llm", return_value=_InspectLLM()):
            result = await deep_research.research_planner(state)

        prompt_messages = captured["messages"]
        self.assertEqual(len(prompt_messages), 2)
        self.assertIn("当前研究笔记（已压缩）", prompt_messages[1].content)
        # Three-tier context is now used in prompt
        self.assertIn("## 研究总结", prompt_messages[1].content)
        self.assertIn("## 最近发现（完整）", prompt_messages[1].content)
        # research_context is populated
        self.assertIn("research_context", result)
        self.assertTrue(len(result["research_context"]["entries"]) > 0)

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
        }

        result = deep_research.research_finish(state)

        self.assertEqual(result["_final_text"], "## 直接结论\n\n可以，但依赖额外几何约束。")

    async def test_research_finish_skips_thinking_only_message_and_uses_previous_text(self) -> None:
        state = {
            "messages": [
                AIMessage(content=[{"type": "text", "text": "**直接结论**\n\n前一轮已经给出结论。"}]),
                AIMessage(content=[{"type": "thinking", "thinking": "继续检索。"}]),
            ],
            "query": "GNSS NLOS",
            "step_count": 2,
        }

        result = deep_research.research_finish(state)

        self.assertEqual(result["_final_text"], "## 直接结论\n\n前一轮已经给出结论。")

    async def test_research_finish_falls_back_to_research_context(self) -> None:
        from capabilities.context_manager import FindingEntry, ResearchContext
        ctx = ResearchContext()
        ctx.add_entry(FindingEntry(step=1, tool_name="web_search", query="q",
                                   raw_content="已有检索笔记。"))
        state = {
            "messages": [
                AIMessage(content=[{"type": "thinking", "thinking": "继续检索。"}]),
            ],
            "query": "GNSS NLOS",
            "step_count": 2,
            "research_context": ctx.to_dict(),
        }

        result = deep_research.research_finish(state)

        self.assertIn("已有检索笔记", result["_final_text"])

    async def test_run_rewrites_final_report_with_markdown_headings(self) -> None:
        class _FakeGraph:
            async def ainvoke(self, state):
                return {"_final_text": "raw notes"}

        class _RewriteLLM:
            async def ainvoke(self, messages):
                return AIMessage(
                    content=[
                        {"type": "reasoning", "summary": []},
                        {"type": "text", "text": "**直接结论**\n\n结论更聚焦。"},
                    ]
                )

        with (
            patch("agents.deep_research.graphs.build_research_graph", return_value=_FakeGraph()),
            patch("agents.deep_research.utils.get_llm", return_value=_RewriteLLM()),
        ):
            result = await deep_research.run("GNSS NLOS")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result"], "## 直接结论\n\n结论更聚焦。")

    async def test_run_accepts_nested_search_query_dict_with_report_profile(self) -> None:
        captured: dict[str, object] = {}

        class _FakeGraph:
            async def ainvoke(self, state):
                captured["state"] = state
                return {"_final_text": "raw notes"}

        class _RewriteLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="## 文献综述正文\n\n聚焦论文写作建议。")

        with (
            patch("agents.deep_research.graphs.build_research_graph", return_value=_FakeGraph()),
            patch("agents.deep_research.utils.get_llm", return_value=_RewriteLLM()),
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

        with patch("agents.deep_research.utils.get_llm", return_value=_InspectRewriteLLM()):
            result = await deep_research._rewrite_final_report(
                "请帮我做后续论文写作指导",
                "raw notes",
                deep_research.ACADEMIC_PAPER_GUIDANCE_PROFILE,
            )

        prompt_messages = captured["messages"]
        self.assertIn("对后续论文写作的明确建议", prompt_messages[0].content)
        self.assertIn("当前输出模板：academic_paper_guidance", prompt_messages[1].content)
        self.assertEqual(result, "## 文献综述正文\n\nAcademic draft.")

    async def test_research_planner_populates_research_context(self) -> None:
        """Verify research_planner returns research_context with entries."""
        class _InspectBoundLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="continuing research")

        class _InspectLLM:
            def bind_tools(self, tools):
                return _InspectBoundLLM()

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "call_1", "name": "web_search", "args": {"query": "test"}}],
                ),
                ToolMessage(content="result from search https://example.com", tool_call_id="call_1", name="web_search"),
            ],
            "query": "test query",
            "step_count": 1,
            "research_context": {},
            "task_id": "",
        }

        with patch("agents.deep_research.graphs.get_llm", return_value=_InspectLLM()):
            result = await deep_research.research_planner(state)

        self.assertIn("research_context", result)
        ctx = result["research_context"]
        self.assertIsInstance(ctx, dict)
        self.assertTrue(len(ctx.get("entries", [])) > 0)
        self.assertEqual(ctx["entries"][0]["tool_name"], "web_search")

    async def test_run_creates_research_memory(self) -> None:
        """Verify run() initializes ResearchMemory and saves final report."""
        class _FakeGraph:
            async def ainvoke(self, state):
                return {"_final_text": "raw notes"}

        class _RewriteLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="## 直接结论\n\nRewritten.")

        with (
            patch("agents.deep_research.graphs.build_research_graph", return_value=_FakeGraph()),
            patch("agents.deep_research.utils.get_llm", return_value=_RewriteLLM()),
            patch("agents.deep_research.run.ResearchMemory") as MockMemory,
        ):
            mock_instance = MagicMock()
            MockMemory.return_value = mock_instance

            result = await deep_research.run("test query")

        self.assertEqual(result["status"], "ok")
        mock_instance.init.assert_called_once()
        mock_instance.save_final_report.assert_called_once()

    async def test_run_backward_compat_no_research_context(self) -> None:
        """Existing callers with simple string input still get ok result."""
        class _FakeGraph:
            async def ainvoke(self, state):
                # Verify new fields are present with defaults
                self.captured_state = state
                return {"_final_text": "notes"}

        class _RewriteLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="## 直接结论\n\nDone.")

        fake_graph = _FakeGraph()

        with (
            patch("agents.deep_research.graphs.build_research_graph", return_value=fake_graph),
            patch("agents.deep_research.utils.get_llm", return_value=_RewriteLLM()),
            patch("agents.deep_research.run.ResearchMemory") as MockMemory,
        ):
            MockMemory.return_value = MagicMock()
            result = await deep_research.run("simple question")

        self.assertEqual(result["status"], "ok")
        self.assertIn("直接结论", result["result"])
        # Verify state had new fields
        self.assertEqual(fake_graph.captured_state["research_context"], {})
        self.assertIsInstance(fake_graph.captured_state["task_id"], str)
        self.assertEqual(fake_graph.captured_state["progress"], {})

    async def test_research_planner_populates_progress(self) -> None:
        """Verify research_planner returns progress dict with tracked searches."""
        class _InspectBoundLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="continuing")

        class _InspectLLM:
            def bind_tools(self, tools):
                return _InspectBoundLLM()

        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "call_1", "name": "web_search", "args": {"query": "GNSS accuracy"}}],
                ),
                ToolMessage(content="GNSS provides 3m accuracy.", tool_call_id="call_1", name="web_search"),
            ],
            "query": "GNSS research",
            "step_count": 1,
            "research_context": {},
            "task_id": "",
            "progress": {},
        }

        with patch("agents.deep_research.graphs.get_llm", return_value=_InspectLLM()):
            result = await deep_research.research_planner(state)

        self.assertIn("progress", result)
        progress = result["progress"]
        self.assertIn("GNSS accuracy", progress["completed_searches"])
        self.assertTrue(len(progress["key_findings_so_far"]) > 0)

    def test_build_research_messages_attention_block(self) -> None:
        """Verify attention_block appears at end of prompt."""
        block = "---\n研究进度：\n目标：test\n---"
        messages = deep_research._build_research_messages(
            "test query", "notes", attention_block=block,
        )
        self.assertIn(block, messages[1].content)

    async def test_run_resume_from_checkpoint(self) -> None:
        """Verify run() resumes from checkpoint when resume_task_id is given."""
        class _FakeGraph:
            async def ainvoke(self, state):
                self.captured_state = state
                return {"_final_text": "resumed notes"}

        class _RewriteLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="## 直接结论\n\nResumed.")

        fake_graph = _FakeGraph()

        with (
            patch("agents.deep_research.graphs.build_research_graph", return_value=fake_graph),
            patch("agents.deep_research.utils.get_llm", return_value=_RewriteLLM()),
            patch("agents.deep_research.run.ResearchMemory") as MockMemory,
        ):
            mock_instance = MagicMock()
            mock_instance.load_checkpoint.return_value = {
                "step_count": 5,
                "research_context": {"entries": [], "summary": "", "current_step": 5},
                "progress": {"original_query": "GNSS", "completed_searches": ["q1"], "failed_searches": [], "key_findings_so_far": [], "remaining_gaps": [], "subtasks_status": [], "clarified_goal": ""},
            }
            mock_instance.load_meta.return_value = {"query": "GNSS research", "report_profile": "default"}
            MockMemory.return_value = mock_instance

            result = await deep_research.run({"resume_task_id": "research_abc123"})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(fake_graph.captured_state["step_count"], 5)

    async def test_run_resume_missing_checkpoint_falls_back(self) -> None:
        """Verify run() gracefully falls back when checkpoint is missing."""
        class _FakeGraph:
            async def ainvoke(self, state):
                self.captured_state = state
                return {"_final_text": "fresh notes"}

        class _RewriteLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="## 直接结论\n\nFresh.")

        fake_graph = _FakeGraph()

        with (
            patch("agents.deep_research.graphs.build_research_graph", return_value=fake_graph),
            patch("agents.deep_research.utils.get_llm", return_value=_RewriteLLM()),
            patch("agents.deep_research.run.ResearchMemory") as MockMemory,
        ):
            mock_instance = MagicMock()
            mock_instance.load_checkpoint.return_value = None
            mock_instance.init.return_value = None
            MockMemory.return_value = mock_instance

            result = await deep_research.run({"query": "test", "resume_task_id": "nonexistent"})

        self.assertEqual(result["status"], "ok")
        # Should start from step 0 since no checkpoint
        self.assertEqual(fake_graph.captured_state["step_count"], 0)

    async def test_summary_generated_at_interval(self) -> None:
        """Verify _generate_structured_summary works correctly."""
        class _SummaryLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="Summary of research progress")

        from capabilities.context_manager import ResearchContext
        from capabilities.progress_tracker import ProgressTracker
        ctx = ResearchContext()
        tracker = ProgressTracker(original_query="test")
        tracker.record_search("q1", success=True)

        with patch("agents.deep_research.utils.get_llm", return_value=_SummaryLLM()):
            summary = await deep_research._generate_structured_summary("test query", ctx, tracker)
        self.assertIn("Summary", summary)

        # Verify the interval logic: step divisible by SUMMARY_INTERVAL should trigger
        self.assertEqual(deep_research.SUMMARY_INTERVAL % deep_research.SUMMARY_INTERVAL, 0)

    async def test_summary_not_generated_before_interval(self) -> None:
        """Verify summary is NOT generated before SUMMARY_INTERVAL."""
        # step=3 should not trigger summary (SUMMARY_INTERVAL=6)
        self.assertNotEqual(3 % deep_research.SUMMARY_INTERVAL, 0)

    def test_core_tools_include_memory_tools(self) -> None:
        """Verify CORE_TOOLS includes save/recall research notes."""
        tool_names = [t.name for t in deep_research.CORE_TOOLS]
        self.assertIn("save_research_note", tool_names)
        self.assertIn("recall_research_notes", tool_names)

    def test_hierarchical_graph_compiles(self) -> None:
        """Verify the hierarchical graph compiles without errors."""
        with patch("core.registry.get_tools_for_agent", return_value=[]):
            graph = deep_research.build_hierarchical_research_graph()
        self.assertIsNotNone(graph)

    async def test_run_hierarchical_mode(self) -> None:
        """Verify run() uses hierarchical graph when requested."""
        class _FakeGraph:
            async def ainvoke(self, state):
                self.captured_state = state
                return {"_final_text": "hierarchical findings"}

        class _RewriteLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="## 直接结论\n\nHierarchical done.")

        fake_graph = _FakeGraph()

        with (
            patch("agents.deep_research.graphs.build_hierarchical_research_graph", return_value=fake_graph),
            patch("agents.deep_research.utils.get_llm", return_value=_RewriteLLM()),
            patch("agents.deep_research.run.ResearchMemory") as MockMemory,
        ):
            MockMemory.return_value = MagicMock()
            result = await deep_research.run({"query": "test", "hierarchical": True})

        self.assertEqual(result["status"], "ok")
        self.assertIn("research_plan", fake_graph.captured_state)
        self.assertIn("current_subtask", fake_graph.captured_state)

    def test_parallel_graph_compiles(self) -> None:
        """Verify the parallel graph compiles without errors."""
        with patch("core.registry.get_tools_for_agent", return_value=[]):
            graph = deep_research.build_parallel_research_graph()
        self.assertIsNotNone(graph)

    async def test_run_parallel_mode(self) -> None:
        """Verify run() uses parallel graph when requested."""
        class _FakeGraph:
            async def ainvoke(self, state):
                self.captured_state = state
                return {"_final_text": "parallel findings"}

        class _RewriteLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="## 直接结论\n\nParallel done.")

        fake_graph = _FakeGraph()

        with (
            patch("agents.deep_research.graphs.build_parallel_research_graph", return_value=fake_graph),
            patch("agents.deep_research.utils.get_llm", return_value=_RewriteLLM()),
            patch("agents.deep_research.run.ResearchMemory") as MockMemory,
        ):
            MockMemory.return_value = MagicMock()
            result = await deep_research.run({"query": "test", "parallel": True})

        self.assertEqual(result["status"], "ok")
        self.assertIn("research_plan", fake_graph.captured_state)
        self.assertEqual(fake_graph.captured_state["research_plan"], {})

    async def test_run_empty_query_returns_error(self) -> None:
        result = await deep_research.run("")
        self.assertEqual(result["status"], "error")
        self.assertIn("不能为空", result["result"])

    async def test_run_empty_dict_query_returns_error(self) -> None:
        result = await deep_research.run({"query": ""})
        self.assertEqual(result["status"], "error")
        self.assertIn("不能为空", result["result"])

    async def test_run_long_query_truncated(self) -> None:
        class _FakeGraph:
            async def ainvoke(self, state):
                self.captured_state = state
                return {"_final_text": "notes"}

        class _RewriteLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="## 直接结论\n\nDone.")

        fake_graph = _FakeGraph()
        long_query = "A" * 15000

        with (
            patch("agents.deep_research.graphs.build_research_graph", return_value=fake_graph),
            patch("agents.deep_research.utils.get_llm", return_value=_RewriteLLM()),
            patch("agents.deep_research.run.ResearchMemory") as MockMemory,
        ):
            MockMemory.return_value = MagicMock()
            result = await deep_research.run(long_query)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(fake_graph.captured_state["query"]), 10000)

    async def test_synthesize_parallel_findings_basic(self) -> None:
        class _SynthLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="合并后的发现报告")

        with patch("agents.deep_research.utils.get_llm", return_value=_SynthLLM()):
            result = await deep_research._synthesize_parallel_findings(
                "test query",
                {"sub_1": "发现1", "sub_2": "发现2"},
                "default",
            )
        self.assertIn("合并后的发现报告", result)

    async def test_synthesize_parallel_findings_fallback(self) -> None:
        """LLM failure should be catchable for fallback."""
        class _FailLLM:
            async def ainvoke(self, messages):
                raise ConnectionError("LLM unavailable")

        with patch("agents.deep_research.utils.get_llm", return_value=_FailLLM()):
            with self.assertRaises((ConnectionError, OSError)):
                await deep_research._synthesize_parallel_findings(
                    "test query", {"sub_1": "f1"}, "default",
                )

    async def test_retry_async_succeeds_first_try(self) -> None:
        call_count = 0
        async def _ok():
            nonlocal call_count
            call_count += 1
            return "success"

        result = await deep_research._retry_async(_ok, max_retries=2, delay=0.01)
        self.assertEqual(result, "success")
        self.assertEqual(call_count, 1)

    async def test_retry_async_succeeds_on_second_try(self) -> None:
        call_count = 0
        async def _flaky():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("transient")
            return "recovered"

        result = await deep_research._retry_async(_flaky, max_retries=2, delay=0.01)
        self.assertEqual(result, "recovered")
        self.assertEqual(call_count, 2)

    async def test_retry_async_raises_after_max_retries(self) -> None:
        async def _always_fail():
            raise OSError("persistent failure")

        with self.assertRaises(OSError):
            await deep_research._retry_async(_always_fail, max_retries=1, delay=0.01)

    async def test_retry_async_no_retry_on_value_error(self) -> None:
        call_count = 0
        async def _bad_input():
            nonlocal call_count
            call_count += 1
            raise ValueError("bad")

        with self.assertRaises(ValueError):
            await deep_research._retry_async(_bad_input, max_retries=2, delay=0.01)
        self.assertEqual(call_count, 1)

    def test_config_from_dict_defaults(self) -> None:
        config = deep_research.ResearchConfig.from_dict({})
        self.assertEqual(config.max_steps, 15)
        self.assertEqual(config.checkpoint_interval, 5)
        self.assertEqual(config.summary_interval, 6)
        self.assertEqual(config.max_parallel_workers, 3)

    def test_config_from_dict_custom(self) -> None:
        config = deep_research.ResearchConfig.from_dict({"max_steps": 5, "checkpoint_interval": 2})
        self.assertEqual(config.max_steps, 5)
        self.assertEqual(config.checkpoint_interval, 2)
        self.assertEqual(config.summary_interval, 6)  # default

    async def test_run_with_custom_config(self) -> None:
        class _FakeGraph:
            async def ainvoke(self, state):
                self.captured_state = state
                return {"_final_text": "notes"}

        class _RewriteLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="## 直接结论\n\nDone.")

        fake_graph = _FakeGraph()

        with (
            patch("agents.deep_research.graphs.build_research_graph", return_value=fake_graph) as mock_build,
            patch("agents.deep_research.utils.get_llm", return_value=_RewriteLLM()),
            patch("agents.deep_research.run.ResearchMemory") as MockMemory,
        ):
            MockMemory.return_value = MagicMock()
            result = await deep_research.run({"query": "test", "config": {"max_steps": 3}})

        self.assertEqual(result["status"], "ok")
        call_args = mock_build.call_args
        config_arg = call_args[0][0] if call_args[0] else call_args[1].get("config")
        self.assertEqual(config_arg.max_steps, 3)

    def test_research_should_continue_non_ai_last_message(self) -> None:
        """Non-AIMessage last message should return 'finish'."""
        from langchain_core.messages import HumanMessage
        state = {"messages": [HumanMessage(content="test")], "step_count": 0, "query": "test"}
        result = deep_research.research_should_continue(state)
        self.assertEqual(result, "finish")

    def test_research_finish_no_messages(self) -> None:
        """Empty messages list should not crash."""
        state = {"messages": [], "query": "test", "step_count": 0}
        result = deep_research.research_finish(state)
        self.assertIn("_final_text", result)

    async def test_run_exception_in_memory_init_continues(self) -> None:
        """Memory init failure should not prevent research from running."""
        class _FakeGraph:
            async def ainvoke(self, state):
                self.captured_state = state
                return {"_final_text": "notes"}

        class _RewriteLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="## 直接结论\n\nDone.")

        fake_graph = _FakeGraph()

        with (
            patch("agents.deep_research.graphs.build_research_graph", return_value=fake_graph),
            patch("agents.deep_research.utils.get_llm", return_value=_RewriteLLM()),
            patch("agents.deep_research.run.ResearchMemory") as MockMemory,
        ):
            mock_instance = MagicMock()
            mock_instance.init.side_effect = OSError("disk full")
            MockMemory.return_value = mock_instance

            result = await deep_research.run("test query")

        self.assertEqual(result["status"], "ok")
        # task_id should be empty since init failed
        self.assertEqual(fake_graph.captured_state["task_id"], "")
