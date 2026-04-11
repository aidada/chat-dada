from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.domains.patent.agent import run_patent_domain
from agent.domains.research.orchestrated import run_research_domain_orchestrated
from agent.domains.zero_report.agent import run_zero_report_domain
from agent.runtime.interaction import (
    ask_user,
    reset_preloaded_user_replies,
    reset_task_interaction_handler,
    set_preloaded_user_replies,
    set_task_interaction_handler,
)
from agent.runtime.task_execution import _merge_nested_interrupt_pending
from agent.platform.streaming import extract_checkpoint_id, stream_nested_graph, translate_stream_part


class StreamingAdapterTests(unittest.TestCase):
    def test_nested_interrupt_pending_is_sticky_across_duplicate_question_events(self) -> None:
        self.assertTrue(
            _merge_nested_interrupt_pending(True, {"content": "same question"})
        )
        self.assertTrue(
            _merge_nested_interrupt_pending(False, {"nested_graph": "research_workflow"})
        )

    def test_custom_part_translates_to_existing_event_shape(self) -> None:
        events = translate_stream_part(
            {"type": "custom", "data": {"event_type": "file", "name": "a.txt", "url": "/download/a.txt"}},
            thread_id="task_1",
            domain="research",
            checkpoint_id="ckpt_1",
            trace_metadata={"task_id": "task_1"},
        )
        self.assertEqual(len(events), 1)
        event_type, payload = events[0]
        self.assertEqual(event_type, "file")
        self.assertEqual(payload["type"], "file")
        self.assertEqual(payload["name"], "a.txt")
        self.assertEqual(payload["url"], "/download/a.txt")
        self.assertEqual(payload["thread_id"], "task_1")
        self.assertEqual(payload["domain"], "research")
        self.assertEqual(payload["graph_node"], "root")
        self.assertEqual(payload["checkpoint_id"], "ckpt_1")
        self.assertEqual(payload["trace_metadata"], {"task_id": "task_1"})
        self.assertEqual(payload["stream_part_type"], "custom")
        self.assertEqual(payload["graph_path"], [])


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ask_user_consumes_preloaded_replies_before_interrupting(self) -> None:
        token = set_preloaded_user_replies(["answer a", "answer b"])
        try:
            self.assertEqual(await ask_user("q1"), "answer a")
            self.assertEqual(await ask_user("q2"), "answer b")
        finally:
            reset_preloaded_user_replies(token)

    def test_interrupt_update_translates_to_question(self) -> None:
        class _Interrupt:
            def __init__(self):
                self.value = {"content": "need answer", "context": "ctx"}

        events = translate_stream_part(
            {"type": "updates", "data": {"__interrupt__": (_Interrupt(),)}},
            thread_id="task_2",
            domain="ppt",
            checkpoint_id="ckpt_2",
            trace_metadata={"task_id": "task_2"},
        )
        self.assertEqual(events[0][0], "question")
        self.assertEqual(events[0][1]["content"], "need answer")
        self.assertEqual(events[0][1]["interrupt_type"], "human_input")

    def test_updates_part_translates_to_node_event(self) -> None:
        events = translate_stream_part(
            {"type": "updates", "ns": ("outer",), "data": {"run_patent": {"status": "ok"}}},
            thread_id="task_updates",
            domain="patent",
            checkpoint_id="ckpt_node",
            trace_metadata={"task_id": "task_updates"},
        )
        self.assertEqual(len(events), 1)
        event_type, payload = events[0]
        self.assertEqual(event_type, "node")
        self.assertEqual(payload["node_name"], "run_patent")
        self.assertEqual(payload["status"], "updated")
        self.assertEqual(payload["update"], {"status": "ok"})
        self.assertEqual(payload["graph_node"], "outer")

    def test_messages_tuple_part_translates_to_token(self) -> None:
        class FakeChunk:
            content = "hello"

        events = translate_stream_part(
            {"type": "messages", "data": (FakeChunk(), {"langgraph_node": "writer"})},
            thread_id="task_3",
            domain="research",
            checkpoint_id="ckpt_3",
            trace_metadata={"task_id": "task_3"},
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "token")
        self.assertEqual(events[0][1]["content"], "hello")
        self.assertEqual(events[0][1]["message_metadata"]["langgraph_node"], "writer")
        self.assertEqual(events[0][1]["graph_node"], "writer")

    def test_messages_dict_translates_to_token(self) -> None:
        events = translate_stream_part(
            {"type": "messages", "data": {"content": "world"}},
            thread_id="task_4",
            domain="research",
            checkpoint_id="ckpt_4",
            trace_metadata={"task_id": "task_4"},
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "token")
        self.assertEqual(events[0][1]["content"], "world")

    def test_messages_empty_content_produces_no_events(self) -> None:
        events = translate_stream_part(
            {"type": "messages", "data": {"content": ""}},
            thread_id="task_5",
            domain="research",
            checkpoint_id="ckpt_5",
            trace_metadata={"task_id": "task_5"},
        )
        self.assertEqual(events, [])

    def test_task_start_part_translates_to_task_event(self) -> None:
        events = translate_stream_part(
            {
                "type": "tasks",
                "ns": ("research", "parallel"),
                "data": {
                    "id": "lg_task_1",
                    "name": "parallel_worker",
                    "input": {"subtask": "sub_1"},
                    "triggers": ["channel_a"],
                },
            },
            thread_id="task_6",
            domain="research",
            checkpoint_id="ckpt_task",
            trace_metadata={"task_id": "task_6"},
        )
        self.assertEqual(len(events), 1)
        event_type, payload = events[0]
        self.assertEqual(event_type, "task")
        self.assertEqual(payload["phase"], "start")
        self.assertEqual(payload["status"], "started")
        self.assertEqual(payload["langgraph_task_id"], "lg_task_1")
        self.assertEqual(payload["task_name"], "parallel_worker")

    def test_task_result_part_translates_to_task_event(self) -> None:
        events = translate_stream_part(
            {
                "type": "tasks",
                "data": {
                    "id": "lg_task_2",
                    "name": "parallel_worker",
                    "error": None,
                    "interrupts": [],
                    "result": {"worker_results": [{"status": "ok"}]},
                },
            },
            thread_id="task_7",
            domain="research",
            checkpoint_id="ckpt_task_result",
            trace_metadata={"task_id": "task_7"},
        )
        self.assertEqual(len(events), 1)
        event_type, payload = events[0]
        self.assertEqual(event_type, "task")
        self.assertEqual(payload["phase"], "finish")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["result"], {"worker_results": [{"status": "ok"}]})

    def test_checkpoint_part_translates_to_checkpoint_event(self) -> None:
        events = translate_stream_part(
            {
                "type": "checkpoints",
                "data": {
                    "config": {"configurable": {"checkpoint_id": "cp_999"}},
                    "next": ("persist_summary",),
                    "tasks": [{"id": "task_a"}],
                    "metadata": {"source": "loop"},
                },
            },
            thread_id="task_8",
            domain="research",
            checkpoint_id="",
            trace_metadata={"task_id": "task_8"},
        )
        self.assertEqual(len(events), 1)
        event_type, payload = events[0]
        self.assertEqual(event_type, "checkpoint")
        self.assertEqual(payload["checkpoint_id"], "cp_999")
        self.assertEqual(payload["status"], "saved")
        self.assertEqual(payload["next_nodes"], ["persist_summary"])
        self.assertEqual(payload["checkpoint_tasks"], [{"id": "task_a"}])

    def test_checkpoint_id_is_extracted(self) -> None:
        checkpoint_id = extract_checkpoint_id(
            {
                "type": "checkpoints",
                "data": {"config": {"configurable": {"checkpoint_id": "cp_123"}}},
            }
        )
        self.assertEqual(checkpoint_id, "cp_123")


class NestedGraphStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_graph_events_are_forwarded_to_parent_writer(self) -> None:
        collected: list[dict] = []

        class FakeGraph:
            async def astream(self, input_data, config=None, version=None, stream_mode=None, subgraphs=None):
                class FakeChunk:
                    content = "hello"

                yield {"type": "tasks", "data": {"id": "t1", "name": "worker", "input": {"x": 1}, "triggers": ["start"]}}
                yield {"type": "messages", "data": (FakeChunk(), {"langgraph_node": "worker"})}
                yield {"type": "checkpoints", "data": {"config": {"configurable": {"checkpoint_id": "cp_nested"}}}}
                yield {"type": "values", "data": {"messages": []}}

        with patch("langgraph.config.get_stream_writer", return_value=collected.append):
            result = await stream_nested_graph(
                FakeGraph(),
                {"messages": []},
                extra_payload={"nested_graph": "demo_nested"},
            )

        self.assertEqual(result, {"messages": []})
        self.assertEqual([payload["event_type"] for payload in collected], ["task", "token", "checkpoint"])
        self.assertEqual(collected[0]["nested_graph"], "demo_nested")
        self.assertEqual(collected[1]["content"], "hello")
        self.assertEqual(collected[2]["checkpoint_id"], "cp_nested")

    async def test_nested_graph_falls_back_to_state_snapshot_when_values_missing(self) -> None:
        class FakeStateSnapshot:
            values = {"final_result": "from snapshot", "aggregated_draft": "draft"}

        class FakeGraph:
            async def astream(self, input_data, config=None, version=None, stream_mode=None, subgraphs=None):
                yield {"type": "custom", "data": {"event_type": "step", "content": "running"}}

            async def aget_state(self, config):
                return FakeStateSnapshot()

        result = await stream_nested_graph(FakeGraph(), {"messages": []}, config={"configurable": {"thread_id": "t1"}})
        self.assertEqual(result, {"final_result": "from snapshot", "aggregated_draft": "draft"})

    async def test_nested_graph_merges_update_payloads_into_final_values(self) -> None:
        class FakeGraph:
            async def astream(self, input_data, config=None, version=None, stream_mode=None, subgraphs=None):
                yield {
                    "type": "updates",
                    "data": {
                        "synthesize_final": {
                            "final_result": "from updates",
                            "aggregated_draft": "draft from updates",
                        }
                    },
                }

        result = await stream_nested_graph(FakeGraph(), {"messages": []})
        self.assertEqual(
            result,
            {"final_result": "from updates", "aggregated_draft": "draft from updates"},
        )

    async def test_nested_graph_propagates_interrupts_to_parent_graph(self) -> None:
        class FakeInterrupt:
            value = {
                "content": "need answer",
                "context": "ctx",
                "interrupt_type": "human_input",
            }

        class FakeGraph:
            async def astream(self, input_data, config=None, version=None, stream_mode=None, subgraphs=None):
                yield {"type": "updates", "data": {"__interrupt__": (FakeInterrupt(),)}}

        with patch("agent.platform.interrupts.request_interrupt", side_effect=RuntimeError("propagated")) as mocked:
            with self.assertRaisesRegex(RuntimeError, "propagated"):
                await stream_nested_graph(FakeGraph(), {"messages": []})
        mocked.assert_called_once_with(
            {
                "content": "need answer",
                "context": "ctx",
                "interrupt_type": "human_input",
            }
        )

    async def test_nested_graph_syncs_consumed_parent_interrupts_from_config(self) -> None:
        class FakeGraph:
            async def astream(self, input_data, config=None, version=None, stream_mode=None, subgraphs=None):
                if False:
                    yield None
                return

        with (
            patch("langgraph.config.get_config", return_value={"configurable": {"nested_interrupt_count": 2}}),
            patch("agent.platform.streaming._sync_parent_interrupt_state") as mocked_sync,
        ):
            result = await stream_nested_graph(FakeGraph(), {"messages": []})

        self.assertIsNone(result)
        mocked_sync.assert_called_once_with(2)

    async def test_nested_graph_uses_resume_command_when_config_provides_resume_value(self) -> None:
        seen = {}

        class FakeGraph:
            async def astream(self, input_data, config=None, version=None, stream_mode=None, subgraphs=None):
                seen["input_data"] = input_data
                if False:
                    yield None
                return

        with patch("langgraph.config.get_config", return_value={"configurable": {"nested_resume_value": "reply"}}):
            await stream_nested_graph(FakeGraph(), {"messages": []})

        self.assertEqual(getattr(seen["input_data"], "resume", None), "reply")


class ResearchDomainTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_domain_wrapper_returns_reviewed_artifacts(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock
        from agent.capabilities.memory import ResearchMemory as BaseResearchMemory

        with TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)

            def _memory_factory(task_id: str):
                return BaseResearchMemory(task_id, root=tmp_root)

            with (
                patch(
                    "agent.domains.research.orchestrated.stream_nested_graph",
                    new=AsyncMock(
                        return_value={
                            "final_result": "## 文献综述正文\n\n研究结果 https://example.com/paper",
                            "aggregated_draft": "## 草案\n\n中间稿",
                            "workflow_trace": ["intake", "planner", "dispatch_modules", "aggregate_draft", "evaluate_draft", "synthesize_final"],
                            "plan": {"modules": [{"module_id": "related_work", "title": "相关工作"}]},
                            "module_outputs": {"related_work": {"content": "文献条目 https://example.com/paper"}},
                            "evaluations": [{"passed": True, "issues": []}],
                            "budget": {"status": "active", "soft_budget_total": 3, "hard_budget_total": 5},
                        }
                    ),
                ) as mocked,
                patch("agent.domains.research.orchestrated.ResearchMemory", side_effect=_memory_factory),
            ):
                result = await run_research_domain_orchestrated({"query": "test query", "task_id": "research_test"})
                report_exists = (tmp_root / "research_test" / "final_report.md").exists()

        mocked.assert_awaited_once()
        self.assertEqual(result.status, "ok")
        self.assertIn("研究结果", result.result)
        self.assertTrue(result.review["passed"])
        self.assertTrue(any(ref["name"] == "final_report.md" for ref in result.artifact_refs))
        self.assertTrue(any(ref["name"] == "evidence.json" for ref in result.artifact_refs))
        self.assertTrue(any(ref["name"] == "budget.json" for ref in result.artifact_refs))
        self.assertTrue(any(ref["name"] == "final_report.md" and ref["path"] == "final_report.md" for ref in result.artifact_refs))
        self.assertTrue(
            any(
                ref["name"] == "final_report.md"
                and ref["url"] == "/tasks/research_test/artifact-file?path=final_report.md"
                for ref in result.artifact_refs
            )
        )
        self.assertTrue(report_exists)

    async def test_deepagents_builder_uses_subagents(self) -> None:
        self.skipTest("research domain no longer exposes a deepagents compatibility builder")

    async def test_research_orchestrated_wrapper_uses_stream_bridge(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock, patch
        from agent.capabilities.memory import ResearchMemory as BaseResearchMemory

        with TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)

            def _memory_factory(task_id: str):
                return BaseResearchMemory(task_id, root=tmp_root)

            with (
                patch(
                    "agent.domains.research.orchestrated.stream_nested_graph",
                    new=AsyncMock(
                        return_value={
                            "final_result": "research final https://example.com",
                            "step_history": [{"strategy": "planning"}, {"strategy": "sequential"}],
                            "evaluations": [{"passed": True, "issues": []}],
                            "budget": {"status": "active", "soft_budget_total": 2, "hard_budget_total": 4},
                        }
                    ),
                ) as mocked,
                patch("agent.domains.research.orchestrated.ResearchMemory", side_effect=_memory_factory),
            ):
                from agent.domains.research.orchestrated import run_research_domain_orchestrated

                result = await run_research_domain_orchestrated({"query": "研究主题", "task_id": "task_r"})
                report_exists = (tmp_root / "task_r" / "final_report.md").exists()

        mocked.assert_awaited_once()
        self.assertEqual(result.status, "ok")
        self.assertIn("research final", result.result)
        self.assertEqual(result.strategy, "research_workflow(planning → sequential)")
        self.assertTrue(any(ref["name"] == "final_report.md" for ref in result.artifact_refs))
        self.assertTrue(any(ref["name"] == "evidence.json" for ref in result.artifact_refs))
        self.assertTrue(any(ref["name"] == "budget.json" for ref in result.artifact_refs))
        self.assertTrue(any(ref["name"] == "final_report.md" and ref["path"] == "final_report.md" for ref in result.artifact_refs))
        self.assertTrue(
            any(
                ref["name"] == "final_report.md"
                and ref["url"] == "/tasks/task_r/artifact-file?path=final_report.md"
                for ref in result.artifact_refs
            )
        )
        self.assertTrue(report_exists)

    async def test_research_orchestrated_wrapper_falls_back_to_aggregated_draft(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock, patch
        from agent.capabilities.memory import ResearchMemory as BaseResearchMemory

        with TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)

            def _memory_factory(task_id: str):
                return BaseResearchMemory(task_id, root=tmp_root)

            with (
                patch(
                    "agent.domains.research.orchestrated.stream_nested_graph",
                    new=AsyncMock(
                        return_value={
                            "aggregated_draft": "## 中间稿\n\n可作为最终兜底输出",
                            "evaluations": [{"passed": True, "issues": []}],
                        }
                    ),
                ),
                patch("agent.domains.research.orchestrated.ResearchMemory", side_effect=_memory_factory),
            ):
                result = await run_research_domain_orchestrated({"query": "研究主题", "task_id": "task_r_fallback"})

        self.assertEqual(result.status, "ok")
        self.assertIn("可作为最终兜底输出", result.result)

    async def test_research_orchestrated_fast_forwards_checkpoint_c_accept(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock, patch

        from agent.capabilities.memory import ResearchMemory as BaseResearchMemory
        from agent.domains.research.workflow import CHECKPOINT_C_PROMPT

        with TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)

            def _memory_factory(task_id: str):
                return BaseResearchMemory(task_id, root=tmp_root)

            memory = _memory_factory("task_checkpoint_c_accept")
            memory.init("研究主题", "")
            (memory.task_dir / "aggregated_draft.md").write_text(
                "## 成稿前确认稿\n\n当前聚合草稿。",
                encoding="utf-8",
            )
            memory.save_checkpoint(
                2,
                {
                    "evaluation": {"passed": True, "summary": "模块评审已通过", "issues": []},
                    "module_outputs": {
                        "argument_map": {"content": "论证链 https://example.com/paper"}
                    },
                    "blocked_modules": [],
                    "budget": {"status": "active", "soft_budget_total": 2, "hard_budget_total": 4},
                },
            )

            with (
                patch(
                    "agent.domains.research.orchestrated.stream_nested_graph",
                    new=AsyncMock(),
                ) as mocked_stream,
                patch(
                    "agent.domains.research.orchestrated.synthesize_final_payload",
                    new=AsyncMock(
                        return_value={
                            "final_result": "## 最终研究输出\n\n已直接收束到最终稿。",
                            "workflow_trace": ["checkpoint_c_accept_resume", "synthesize_final"],
                        }
                    ),
                ) as mocked_synth,
                patch("agent.domains.research.orchestrated.ResearchMemory", side_effect=_memory_factory),
            ):
                result = await run_research_domain_orchestrated(
                    {
                        "query": "研究主题",
                        "task_id": "task_checkpoint_c_accept",
                        "clarification_history": [
                            {
                                "question": CHECKPOINT_C_PROMPT,
                                "answer": "无补充，继续",
                                "nested_graph": "research_workflow",
                            }
                        ],
                    }
                )

        mocked_stream.assert_not_awaited()
        mocked_synth.assert_awaited_once()
        self.assertEqual(result.status, "ok")
        self.assertIn("已直接收束到最终稿", result.result)
        self.assertTrue(result.review["passed"])
        self.assertTrue(any(ref["name"] == "final_report.md" for ref in result.artifact_refs))


class PatentDomainTests(unittest.IsolatedAsyncioTestCase):
    async def test_patent_domain_produces_structured_artifacts(self) -> None:
        task_id = "patent_test_basic"
        task_dir = Path("data/patent") / task_id
        if task_dir.exists():
            shutil.rmtree(task_dir)

        try:
            result = await run_patent_domain(
                {
                    "task_id": task_id,
                    "query": "为一种 GNSS 多路径抑制方法整理技术交底并生成专利草稿",
                    "use_deepagents": False,
                }
            )
        finally:
            if task_dir.exists():
                shutil.rmtree(task_dir)

        self.assertEqual(result.status, "ok")
        self.assertIn("权利要求树", result.result)
        self.assertTrue(result.review["passed"])
        self.assertTrue(any(ref["name"] == "claim_tree.json" for ref in result.artifact_refs))
        self.assertTrue(any(ref["name"] == "evidence.json" for ref in result.artifact_refs))
        self.assertEqual(result.budget["action"], "allow")

    async def test_deepagents_patent_builder_uses_subagents(self) -> None:
        with (
            patch("deepagents.create_deep_agent") as mocked,
            patch("core.models.build_chat_model", return_value=object()),
        ):
            from agent.domains.patent.agent import build_deepagents_patent_agent

            mocked.return_value = object()
            result = await build_deepagents_patent_agent()

        self.assertIsNotNone(result)
        self.assertTrue(mocked.called)
        call_kwargs = mocked.call_args[1]
        self.assertEqual(len(call_kwargs["subagents"]), 5)
        names = {s["name"] for s in call_kwargs["subagents"]}
        self.assertIn("technical_disclosure_analyst", names)
        self.assertIn("prior_art_researcher", names)
        self.assertIn("claim_drafter", names)
        self.assertIn("specification_drafter", names)
        self.assertIn("patent_reviewer", names)

    async def test_patent_orchestrated_wrapper_uses_stream_bridge(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock, patch

        with TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            with (
                patch(
                    "agent.domains.patent.orchestrated.stream_nested_graph",
                    new=AsyncMock(
                        return_value={
                            "final_result": "patent final",
                            "step_history": [{"strategy": "sequential"}],
                            "evaluations": [{"passed": True, "issues": []}],
                        }
                    ),
                ) as mocked,
                patch("agent.domains.patent.agent.PATENT_DATA_ROOT", tmp_root),
                patch("agent.domains.patent.orchestrated.PATENT_DATA_ROOT", tmp_root),
            ):
                from agent.domains.patent.orchestrated import run_patent_domain_orchestrated

                result = await run_patent_domain_orchestrated({"query": "专利任务", "task_id": "task_p"})
                report_exists = (tmp_root / "task_p" / "patent_draft.md").exists()

        mocked.assert_awaited_once()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.result, "patent final")
        self.assertIn("orchestrated(sequential)", result.budget["reason"])
        self.assertTrue(any(ref["name"] == "patent_draft.md" for ref in result.artifact_refs))
        self.assertTrue(any(ref["name"] == "claim_tree.json" for ref in result.artifact_refs))
        self.assertTrue(report_exists)


class ZeroReportDomainTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_report_domain_produces_structured_artifacts(self) -> None:
        task_id = "zero_report_test_basic"
        task_dir = Path("data/zero_report") / task_id
        if task_dir.exists():
            shutil.rmtree(task_dir)

        try:
            result = await run_zero_report_domain(
                {
                    "task_id": task_id,
                    "query": "针对某次线上事故生成归零报告、时间线和整改矩阵",
                    "use_deepagents": False,
                }
            )
        finally:
            if task_dir.exists():
                shutil.rmtree(task_dir)

        self.assertEqual(result.status, "ok")
        self.assertIn("时间线", result.result)
        self.assertTrue(result.review["passed"])
        self.assertTrue(any(ref["name"] == "timeline.json" for ref in result.artifact_refs))
        self.assertTrue(any(ref["name"] == "evidence.json" for ref in result.artifact_refs))
        self.assertEqual(result.budget["action"], "allow")

    async def test_deepagents_zero_report_builder_uses_subagents(self) -> None:
        with (
            patch("deepagents.create_deep_agent") as mocked,
            patch("core.models.build_chat_model", return_value=object()),
        ):
            from agent.domains.zero_report.agent import build_deepagents_zero_report_agent

            mocked.return_value = object()
            result = await build_deepagents_zero_report_agent()

        self.assertIsNotNone(result)
        self.assertTrue(mocked.called)
        call_kwargs = mocked.call_args[1]
        self.assertEqual(len(call_kwargs["subagents"]), 5)
        names = {s["name"] for s in call_kwargs["subagents"]}
        self.assertIn("incident_structurer", names)
        self.assertIn("timeline_builder", names)
        self.assertIn("root_cause_analyst", names)
        self.assertIn("corrective_action_planner", names)
        self.assertIn("report_reviewer", names)

    async def test_zero_report_orchestrated_wrapper_uses_stream_bridge(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock, patch

        with TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            with (
                patch(
                    "agent.domains.zero_report.orchestrated.stream_nested_graph",
                    new=AsyncMock(
                        return_value={
                            "final_result": "zero report final",
                            "step_history": [{"strategy": "planning"}, {"strategy": "iterative"}],
                            "evaluations": [{"passed": False, "issues": [{"message": "missing owner"}]}],
                        }
                    ),
                ) as mocked,
                patch("agent.domains.zero_report.agent.ZERO_REPORT_DATA_ROOT", tmp_root),
                patch("agent.domains.zero_report.orchestrated.ZERO_REPORT_DATA_ROOT", tmp_root),
            ):
                from agent.domains.zero_report.orchestrated import run_zero_report_domain_orchestrated

                result = await run_zero_report_domain_orchestrated({"query": "归零任务", "task_id": "task_z"})
                report_exists = (tmp_root / "task_z" / "zero_report.md").exists()

        mocked.assert_awaited_once()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.result, "zero report final")
        self.assertFalse(result.review["passed"])
        self.assertIn("orchestrated(planning → iterative)", result.budget["reason"])
        self.assertTrue(any(ref["name"] == "zero_report.md" for ref in result.artifact_refs))
        self.assertTrue(any(ref["name"] == "timeline.json" for ref in result.artifact_refs))
        self.assertTrue(report_exists)


class InteractionHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ask_user_returns_none_with_no_bridge_and_no_handler(self) -> None:
        """With no graph interrupt bridge and handler set to None, ask_user returns None."""
        token = set_task_interaction_handler(None)
        try:
            result = await ask_user("test question")
            self.assertIsNone(result)
        finally:
            reset_task_interaction_handler(token)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recover_interrupted_tasks_reads_candidate_ids_from_pool(self) -> None:
        """_recover_interrupted_tasks should return candidate ids from the live pool."""
        from unittest.mock import AsyncMock, MagicMock

        store = MagicMock()
        store.pool = AsyncMock()
        store.pool.fetch = AsyncMock(return_value=[
            {"task_id": "task_running"},
            {"task_id": "task_waiting"},
        ])

        from agent.runtime.task_execution import TaskRunStore
        task_ids = await TaskRunStore._recover_interrupted_tasks(store)

        self.assertEqual(task_ids, ["task_running", "task_waiting"])
        store.pool.fetch.assert_awaited_once()

    async def test_recover_interrupted_tasks_falls_back_to_repository_without_pool(self) -> None:
        """_recover_interrupted_tasks should use TaskRunRepository when no pool is attached."""
        from unittest.mock import AsyncMock, MagicMock, patch

        session_cm = AsyncMock()
        session = MagicMock()
        session_cm.__aenter__.return_value = session
        session_cm.__aexit__.return_value = False
        repo = MagicMock()
        repo.list_interrupted = AsyncMock(return_value=["task_1"])

        from agent.runtime.task_execution import TaskRunStore

        with patch("agent.runtime.task_execution.SessionFactory", return_value=session_cm), \
             patch("agent.runtime.task_execution.TaskRunRepository", return_value=repo):
            task_ids = await TaskRunStore("postgresql://example")._recover_interrupted_tasks()

        self.assertEqual(task_ids, ["task_1"])
        repo.list_interrupted.assert_awaited_once()


class ResumePathTests(unittest.TestCase):
    def test_resume_reads_execution_path_from_request_payload(self) -> None:
        """On resume, execution_path should come from request_payload, not route_name."""
        snapshot = {
            "route_name": "ppt",
            "route_reason": "test",
            "route_confidence": 0.9,
            "request_payload": {
                "execution_path": "ppt",
                "domain": "ppt",
            },
        }
        # Simulate the resume path logic
        request_payload = snapshot.get("request_payload", {})
        execution_path = request_payload.get(
            "execution_path",
            snapshot.get("route_name", ""),
        )
        self.assertEqual(execution_path, "ppt")


class RootGraphInterruptResumeTests(unittest.IsolatedAsyncioTestCase):
    """Integration tests: root graph interrupt → resume via Command(resume=...)."""

    async def test_interrupt_and_resume_through_root_graph(self) -> None:
        from langgraph.checkpoint.memory import InMemorySaver
        from agent.runtime.root_graph import build_root_graph

        checkpointer = InMemorySaver()
        graph = build_root_graph(checkpointer=checkpointer)

        initial_state = {
            "task_id": "test_interrupt_resume",
            "thread_id": "test_interrupt_resume",
            "user_id": "test_user",
            "mode": "auto",
            "thinking_level": "medium",
            "task_text": "短",
            "execution_task": "短",
            "file_paths": [],
            "conversation_id": "",
            "conversation_context": "",
            "request_payload": {},
            "initial_route_payload": {"execution_path": "needs_clarification"},
        }
        config = {"configurable": {"thread_id": "test_interrupt_resume"}}

        # First run: explicit needs_clarification route should interrupt at root graph level
        interrupted = False
        events_collected = []
        async for part in graph.astream(
            initial_state,
            config=config,
            version="v2",
            stream_mode=["updates"],
        ):
            data = part.get("data") or {}
            if data.get("__interrupt__"):
                interrupted = True
                break
            events_collected.append(part)

        self.assertTrue(interrupted, "Graph should have interrupted for clarification")

    async def test_nested_graph_can_interrupt_twice_and_then_finish_via_resume_config(self) -> None:
        from unittest.mock import patch

        class FakeInterrupt:
            def __init__(self, value):
                self.value = value

        class FakeNestedGraph:
            def __init__(self):
                self.phases: dict[str, int] = {}

            async def astream(self, input_data, config=None, version=None, stream_mode=None, subgraphs=None):
                thread_id = str((config or {}).get("configurable", {}).get("thread_id", "default"))
                phase = self.phases.get(thread_id, 0)
                if phase == 0:
                    self.phases[thread_id] = 1
                    yield {
                        "type": "updates",
                        "data": {
                            "__interrupt__": (
                                FakeInterrupt({"content": "q1", "interrupt_type": "human_input"}),
                            )
                        },
                    }
                    return
                if phase == 1:
                    self.phases[thread_id] = 2
                    yield {
                        "type": "updates",
                        "data": {
                            "__interrupt__": (
                                FakeInterrupt({"content": "q2", "interrupt_type": "human_input"}),
                            )
                        },
                    }
                    return
                self.phases[thread_id] = 3
                yield {
                    "type": "updates",
                    "data": {
                        "synthesize_final": {
                            "final_result": "nested final result",
                            "artifact_refs": [],
                        }
                    },
                }
                yield {
                    "type": "values",
                    "data": {
                        "final_result": "nested final result",
                        "artifact_refs": [],
                    },
                }

        nested_graph = FakeNestedGraph()
        config = {"configurable": {"thread_id": "nested_interrupt_resume"}}

        with patch("agent.platform.interrupts.request_interrupt", side_effect=RuntimeError("q1")) as request_interrupt:
            with self.assertRaisesRegex(RuntimeError, "q1"):
                await stream_nested_graph(nested_graph, {"query": "研究 test nested interrupt flow"}, config=config)
        request_interrupt.assert_called_once_with({"content": "q1", "interrupt_type": "human_input"})

        with patch(
            "langgraph.config.get_config",
            return_value={"configurable": {"nested_interrupt_count": 1, "nested_resume_value": "answer1"}},
        ), patch("agent.platform.interrupts.request_interrupt", side_effect=RuntimeError("q2")) as request_interrupt:
            with self.assertRaisesRegex(RuntimeError, "q2"):
                await stream_nested_graph(nested_graph, {"query": "研究 test nested interrupt flow"}, config=config)
        request_interrupt.assert_called_once_with({"content": "q2", "interrupt_type": "human_input"})

        with patch(
            "langgraph.config.get_config",
            return_value={"configurable": {"nested_interrupt_count": 1, "nested_resume_value": "answer2"}},
        ):
            result = await stream_nested_graph(
                nested_graph,
                {"query": "研究 test nested interrupt flow"},
                config=config,
            )

        self.assertEqual(result["final_result"], "nested final result")


class CitationMapTests(unittest.TestCase):
    def test_add_deduplicates_by_url(self) -> None:
        from agent.capabilities.citation_manager import CitationMap

        cm = CitationMap()
        c1 = cm.add("https://example.com/a", title="Page A")
        c2 = cm.add("https://example.com/a", title="Page A duplicate")
        self.assertIs(c1, c2)
        self.assertEqual(len(cm.all()), 1)

    def test_sequential_numbering(self) -> None:
        from agent.capabilities.citation_manager import CitationMap

        cm = CitationMap()
        c1 = cm.add("https://a.com")
        c2 = cm.add("https://b.com")
        c3 = cm.add("https://c.com")
        self.assertEqual(c1.citation_id, 1)
        self.assertEqual(c2.citation_id, 2)
        self.assertEqual(c3.citation_id, 3)

    def test_render_footnotes(self) -> None:
        from agent.capabilities.citation_manager import CitationMap

        cm = CitationMap()
        cm.add("https://a.com", title="Site A")
        cm.add("https://b.com")
        text = cm.render_footnotes()
        self.assertIn("[1] Site A", text)
        self.assertIn("[2] https://b.com", text)

    def test_render_markdown_references(self) -> None:
        from agent.capabilities.citation_manager import CitationMap

        cm = CitationMap()
        cm.add("https://a.com", title="Site A")
        md = cm.render_markdown_references()
        self.assertIn("## References", md)
        self.assertIn("[Site A](https://a.com)", md)

    def test_to_dicts(self) -> None:
        from agent.capabilities.citation_manager import CitationMap

        cm = CitationMap()
        cm.add("https://a.com", title="A")
        dicts = cm.to_dicts()
        self.assertEqual(len(dicts), 1)
        self.assertEqual(dicts[0]["url"], "https://a.com")
        self.assertEqual(dicts[0]["citation_id"], 1)

    def test_url_normalization(self) -> None:
        from agent.capabilities.citation_manager import CitationMap

        cm = CitationMap()
        c1 = cm.add("https://example.com/path/")
        c2 = cm.add("https://example.com/path")
        self.assertIs(c1, c2)


class EvidenceCollectionTests(unittest.TestCase):
    def test_add_and_filter_by_type(self) -> None:
        from agent.capabilities.evidence_store import EvidenceCollection, EvidenceItem

        ec = EvidenceCollection(task_id="test")
        ec.add(EvidenceItem(evidence_id="1", evidence_type="url", source="https://a.com"))
        ec.add(EvidenceItem(evidence_id="2", evidence_type="quote", source="some text"))
        ec.add(EvidenceItem(evidence_id="3", evidence_type="url", source="https://b.com"))

        urls = ec.by_type("url")
        self.assertEqual(len(urls), 2)
        quotes = ec.by_type("quote")
        self.assertEqual(len(quotes), 1)

    def test_sources_are_deduplicated_and_ordered(self) -> None:
        from agent.capabilities.evidence_store import EvidenceCollection, EvidenceItem

        ec = EvidenceCollection(task_id="test")
        ec.add(EvidenceItem(evidence_id="1", evidence_type="url", source="https://a.com"))
        ec.add(EvidenceItem(evidence_id="2", evidence_type="url", source="https://b.com"))
        ec.add(EvidenceItem(evidence_id="3", evidence_type="url", source="https://a.com"))
        sources = ec.sources()
        self.assertEqual(sources, ["https://a.com", "https://b.com"])


class ResearchEvidenceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_builds_evidence_from_urls_in_report(self) -> None:
        from agent.domains.research.utils import build_evidence_and_citations

        evidence, citations = build_evidence_and_citations(
            "test_task",
            "Report with https://example.com/paper1 and https://example.com/paper2 as sources.",
        )
        self.assertEqual(len(evidence.items), 2)
        self.assertEqual(len(citations.all()), 2)
        self.assertIn("https://example.com/paper1", evidence.sources())

    async def test_research_builds_evidence_from_worker_results(self) -> None:
        from agent.domains.research.utils import build_evidence_and_citations

        workers = [
            {"subtask_id": "s1", "topic": "ML", "findings": "Found at https://arxiv.org/abs/1234"},
            {"subtask_id": "s2", "topic": "NLP", "findings": "No URLs here"},
        ]
        evidence, citations = build_evidence_and_citations("test_task", "final text", workers)
        url_evidence = evidence.by_type("url")
        self.assertTrue(any("arxiv" in e.source for e in url_evidence))
        self.assertTrue(any(c.url == "https://arxiv.org/abs/1234" for c in citations.all()))
