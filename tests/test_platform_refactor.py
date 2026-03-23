from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from domain_agents.patent.agent import run_patent_domain
from domain_agents.research.orchestrated import run_research_domain_orchestrated
from domain_agents.zero_report.agent import run_zero_report_domain
from runtime.task_dispatcher import RouteDecision
from runtime.task_dispatcher import run_general_chat_task
from runtime.task_interaction import ask_user, reset_task_interaction_handler, set_task_interaction_handler
from task_platform.router import build_route_payload
from task_platform.streaming import extract_checkpoint_id, stream_nested_graph, translate_stream_part


class StreamingAdapterTests(unittest.TestCase):
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


class GeneralChatStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_general_chat_emits_token_and_result_delta(self) -> None:
        collected: list[str] = []

        async def fake_on_step(step: str) -> None:
            collected.append(step)

        async def fake_run_general_chat(input_data, on_chunk=None):
            if on_chunk is not None:
                await on_chunk("partial")
            return {"result": "done"}

        with patch("runtime.task_dispatcher.run_general_chat", side_effect=fake_run_general_chat):
            result = await run_general_chat_task("hello", fake_on_step, user_id="u1")

        self.assertEqual(result, "done")
        self.assertEqual(len(collected), 3)
        self.assertEqual(collected[0], "💬 正在回答...")
        self.assertIn('"type": "token"', collected[1])
        self.assertIn('"type": "result_delta"', collected[2])


class ResearchDomainTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_domain_wrapper_returns_reviewed_artifacts(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock
        from capabilities.memory import ResearchMemory as BaseResearchMemory

        with TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)

            def _memory_factory(task_id: str):
                return BaseResearchMemory(task_id, root=tmp_root)

            with (
                patch(
                    "domain_agents.research.orchestrated.stream_nested_graph",
                    new=AsyncMock(
                        return_value={
                            "final_result": "## 文献综述正文\n\n研究结果 https://example.com/paper",
                            "aggregated_draft": "## 草案\n\n中间稿",
                            "workflow_trace": ["intake", "planner", "dispatch_modules", "aggregate_draft", "evaluate_draft", "synthesize_final"],
                            "plan": {"modules": [{"module_id": "related_work", "title": "相关工作"}]},
                            "module_outputs": {"related_work": {"content": "文献条目 https://example.com/paper"}},
                            "evaluations": [{"passed": True, "issues": []}],
                        }
                    ),
                ) as mocked,
                patch("domain_agents.research.orchestrated.ResearchMemory", side_effect=_memory_factory),
            ):
                result = await run_research_domain_orchestrated({"query": "test query", "task_id": "research_test"})
                report_exists = (tmp_root / "research_test" / "final_report.md").exists()

        mocked.assert_awaited_once()
        self.assertEqual(result.status, "ok")
        self.assertIn("研究结果", result.result)
        self.assertTrue(result.review["passed"])
        self.assertTrue(any(ref["name"] == "final_report.md" for ref in result.artifact_refs))
        self.assertTrue(any(ref["name"] == "evidence.json" for ref in result.artifact_refs))
        self.assertTrue(report_exists)

    async def test_deepagents_builder_uses_subagents(self) -> None:
        self.skipTest("research domain no longer exposes a deepagents compatibility builder")

    async def test_research_orchestrated_wrapper_uses_stream_bridge(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock, patch
        from capabilities.memory import ResearchMemory as BaseResearchMemory

        with TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)

            def _memory_factory(task_id: str):
                return BaseResearchMemory(task_id, root=tmp_root)

            with (
                patch(
                    "domain_agents.research.orchestrated.stream_nested_graph",
                    new=AsyncMock(
                        return_value={
                            "final_result": "research final https://example.com",
                            "step_history": [{"strategy": "planning"}, {"strategy": "sequential"}],
                            "evaluations": [{"passed": True, "issues": []}],
                        }
                    ),
                ) as mocked,
                patch("domain_agents.research.orchestrated.ResearchMemory", side_effect=_memory_factory),
            ):
                from domain_agents.research.orchestrated import run_research_domain_orchestrated

                result = await run_research_domain_orchestrated({"query": "研究主题", "task_id": "task_r"})
                report_exists = (tmp_root / "task_r" / "final_report.md").exists()

        mocked.assert_awaited_once()
        self.assertEqual(result.status, "ok")
        self.assertIn("research final", result.result)
        self.assertEqual(result.strategy, "research_workflow(planning → sequential)")
        self.assertTrue(any(ref["name"] == "final_report.md" for ref in result.artifact_refs))
        self.assertTrue(any(ref["name"] == "evidence.json" for ref in result.artifact_refs))
        self.assertTrue(report_exists)


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
            from domain_agents.patent.agent import build_deepagents_patent_agent

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
                    "domain_agents.patent.orchestrated.stream_nested_graph",
                    new=AsyncMock(
                        return_value={
                            "final_result": "patent final",
                            "step_history": [{"strategy": "sequential"}],
                            "evaluations": [{"passed": True, "issues": []}],
                        }
                    ),
                ) as mocked,
                patch("domain_agents.patent.agent.PATENT_DATA_ROOT", tmp_root),
                patch("domain_agents.patent.orchestrated.PATENT_DATA_ROOT", tmp_root),
            ):
                from domain_agents.patent.orchestrated import run_patent_domain_orchestrated

                result = await run_patent_domain_orchestrated({"query": "专利任务", "task_id": "task_p"})
                report_exists = (tmp_root / "task_p" / "patent_draft.md").exists()

        mocked.assert_awaited_once()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.result, "patent final")
        self.assertIn("orchestrated(sequential)", result.budget["reason"])
        self.assertTrue(any(ref["name"] == "patent_draft.md" for ref in result.artifact_refs))
        self.assertTrue(any(ref["name"] == "claim_tree.json" for ref in result.artifact_refs))
        self.assertTrue(report_exists)

    def test_router_can_select_patent_domain(self) -> None:
        decision = RouteDecision(
            route_name="orchestrator",
            reason="detected patent task",
            confidence=0.9,
        )
        route = build_route_payload(
            task_text="请根据技术交底生成专利权利要求和说明书草稿",
            file_paths=[],
            decision=decision,
        )
        self.assertEqual(route["execution_path"], "patent")


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
            from domain_agents.zero_report.agent import build_deepagents_zero_report_agent

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
                    "domain_agents.zero_report.orchestrated.stream_nested_graph",
                    new=AsyncMock(
                        return_value={
                            "final_result": "zero report final",
                            "step_history": [{"strategy": "planning"}, {"strategy": "iterative"}],
                            "evaluations": [{"passed": False, "issues": [{"message": "missing owner"}]}],
                        }
                    ),
                ) as mocked,
                patch("domain_agents.zero_report.agent.ZERO_REPORT_DATA_ROOT", tmp_root),
                patch("domain_agents.zero_report.orchestrated.ZERO_REPORT_DATA_ROOT", tmp_root),
            ):
                from domain_agents.zero_report.orchestrated import run_zero_report_domain_orchestrated

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

    def test_router_can_select_zero_report_domain(self) -> None:
        decision = RouteDecision(
            route_name="orchestrator",
            reason="detected incident analysis task",
            confidence=0.9,
        )
        route = build_route_payload(
            task_text="请输出事故复盘的时间线、根因分析和整改矩阵",
            file_paths=[],
            decision=decision,
        )
        self.assertEqual(route["execution_path"], "zero_report")


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
    async def test_recover_preserves_waiting_for_user_tasks(self) -> None:
        """_recover_interrupted_tasks should skip waiting_for_user tasks."""
        from unittest.mock import AsyncMock, MagicMock

        store = MagicMock()
        store.pool = AsyncMock()
        store.pool.fetch = AsyncMock(return_value=[
            {"task_id": "task_running"},
            {"task_id": "task_waiting"},
        ])

        async def fake_get_task(task_id):
            if task_id == "task_waiting":
                return {"status": "waiting_for_user"}
            return {"status": "running"}

        store.get_task = AsyncMock(side_effect=fake_get_task)
        store.set_error_text = AsyncMock()
        store.append_event = AsyncMock()
        store.finish_task = AsyncMock()

        from runtime.task_runtime import TaskRunStore
        await TaskRunStore._recover_interrupted_tasks(store)

        store.finish_task.assert_called_once_with("task_running", "failed")
        # waiting_for_user task should NOT be marked failed
        for call in store.finish_task.call_args_list:
            self.assertNotEqual(call[0][0], "task_waiting")

    async def test_recover_marks_running_tasks_as_failed(self) -> None:
        """_recover_interrupted_tasks marks running tasks as failed."""
        from unittest.mock import AsyncMock, MagicMock

        store = MagicMock()
        store.pool = AsyncMock()
        store.pool.fetch = AsyncMock(return_value=[
            {"task_id": "task_1"},
        ])
        store.get_task = AsyncMock(return_value={"status": "running"})
        store.set_error_text = AsyncMock()
        store.append_event = AsyncMock()
        store.finish_task = AsyncMock()

        from runtime.task_runtime import TaskRunStore
        await TaskRunStore._recover_interrupted_tasks(store)

        store.finish_task.assert_called_once_with("task_1", "failed")


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
        from unittest.mock import patch
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.types import Command
        from task_platform.root_graph import build_root_graph

        async def fake_dispatcher(task_text, file_paths, mode, user_id):
            return RouteDecision(
                route_name="orchestrator",
                reason="test interrupt",
                confidence=0.5,
            )

        checkpointer = InMemorySaver()
        graph = build_root_graph(
            dispatcher=fake_dispatcher,
            checkpointer=checkpointer,
        )

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
        }
        config = {"configurable": {"thread_id": "test_interrupt_resume"}}

        # First run: should hit needs_clarification → maybe_clarify → interrupt
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

        # Resume with user answer
        resumed_events = []
        async def fake_research_runner(input_data):
            return type(
                "FakeResearchResult",
                (),
                {
                    "result": f"stubbed research for {input_data['query']}",
                    "artifact_refs": [],
                    "review": {"passed": True},
                    "budget": {"action": "allow"},
                    "strategy": "stubbed",
                },
            )()

        with patch("task_platform.root_graph.domain_registry.get", return_value=fake_research_runner):
            async for part in graph.astream(
                Command(resume="请做深度研究"),
                config=config,
                version="v2",
                stream_mode=["updates"],
            ):
                data = part.get("data") or {}
                if data.get("__interrupt__"):
                    # Second interrupt is acceptable (e.g., domain agent may interrupt)
                    break
                resumed_events.append(part)

        # After resume, graph should have re-routed (route_domain runs again)
        state = await graph.aget_state(config)
        values = getattr(state, "values", {}) or {}
        # The clarification answer should be in request_payload
        rp = values.get("request_payload", {})
        self.assertEqual(rp.get("clarification_answer"), "请做深度研究")

    async def test_non_interrupt_path_completes(self) -> None:
        """High-confidence tasks should complete without interruption."""
        from langgraph.checkpoint.memory import InMemorySaver
        from task_platform.root_graph import build_root_graph

        async def fake_dispatcher(task_text, file_paths, mode, user_id):
            return RouteDecision(
                route_name="general_chat",
                reason="simple question",
                confidence=0.95,
            )

        async def fake_general_chat(task, on_step, **kwargs):
            return "Hello!"

        checkpointer = InMemorySaver()
        graph = build_root_graph(
            dispatcher=fake_dispatcher,
            checkpointer=checkpointer,
        )

        initial_state = {
            "task_id": "test_no_interrupt",
            "thread_id": "test_no_interrupt",
            "user_id": "test_user",
            "mode": "auto",
            "thinking_level": "medium",
            "task_text": "你好",
            "execution_task": "你好",
            "file_paths": [],
            "conversation_id": "",
            "conversation_context": "",
            "request_payload": {},
        }
        config = {"configurable": {"thread_id": "test_no_interrupt"}}

        interrupted = False
        with patch("runtime.task_dispatcher.run_general_chat_task", new=fake_general_chat):
            async for part in graph.astream(
                initial_state,
                config=config,
                version="v2",
                stream_mode=["updates"],
            ):
                data = part.get("data") or {}
                if data.get("__interrupt__"):
                    interrupted = True

        self.assertFalse(interrupted, "High-confidence task should not interrupt")
        state = await graph.aget_state(config)
        values = getattr(state, "values", {}) or {}
        self.assertEqual(values.get("final_result"), "Hello!")


class CitationMapTests(unittest.TestCase):
    def test_add_deduplicates_by_url(self) -> None:
        from capabilities.citation_manager import CitationMap

        cm = CitationMap()
        c1 = cm.add("https://example.com/a", title="Page A")
        c2 = cm.add("https://example.com/a", title="Page A duplicate")
        self.assertIs(c1, c2)
        self.assertEqual(len(cm.all()), 1)

    def test_sequential_numbering(self) -> None:
        from capabilities.citation_manager import CitationMap

        cm = CitationMap()
        c1 = cm.add("https://a.com")
        c2 = cm.add("https://b.com")
        c3 = cm.add("https://c.com")
        self.assertEqual(c1.citation_id, 1)
        self.assertEqual(c2.citation_id, 2)
        self.assertEqual(c3.citation_id, 3)

    def test_render_footnotes(self) -> None:
        from capabilities.citation_manager import CitationMap

        cm = CitationMap()
        cm.add("https://a.com", title="Site A")
        cm.add("https://b.com")
        text = cm.render_footnotes()
        self.assertIn("[1] Site A", text)
        self.assertIn("[2] https://b.com", text)

    def test_render_markdown_references(self) -> None:
        from capabilities.citation_manager import CitationMap

        cm = CitationMap()
        cm.add("https://a.com", title="Site A")
        md = cm.render_markdown_references()
        self.assertIn("## References", md)
        self.assertIn("[Site A](https://a.com)", md)

    def test_to_dicts(self) -> None:
        from capabilities.citation_manager import CitationMap

        cm = CitationMap()
        cm.add("https://a.com", title="A")
        dicts = cm.to_dicts()
        self.assertEqual(len(dicts), 1)
        self.assertEqual(dicts[0]["url"], "https://a.com")
        self.assertEqual(dicts[0]["citation_id"], 1)

    def test_url_normalization(self) -> None:
        from capabilities.citation_manager import CitationMap

        cm = CitationMap()
        c1 = cm.add("https://example.com/path/")
        c2 = cm.add("https://example.com/path")
        self.assertIs(c1, c2)


class EvidenceCollectionTests(unittest.TestCase):
    def test_add_and_filter_by_type(self) -> None:
        from capabilities.evidence_store import EvidenceCollection, EvidenceItem

        ec = EvidenceCollection(task_id="test")
        ec.add(EvidenceItem(evidence_id="1", evidence_type="url", source="https://a.com"))
        ec.add(EvidenceItem(evidence_id="2", evidence_type="quote", source="some text"))
        ec.add(EvidenceItem(evidence_id="3", evidence_type="url", source="https://b.com"))

        urls = ec.by_type("url")
        self.assertEqual(len(urls), 2)
        quotes = ec.by_type("quote")
        self.assertEqual(len(quotes), 1)

    def test_sources_are_deduplicated_and_ordered(self) -> None:
        from capabilities.evidence_store import EvidenceCollection, EvidenceItem

        ec = EvidenceCollection(task_id="test")
        ec.add(EvidenceItem(evidence_id="1", evidence_type="url", source="https://a.com"))
        ec.add(EvidenceItem(evidence_id="2", evidence_type="url", source="https://b.com"))
        ec.add(EvidenceItem(evidence_id="3", evidence_type="url", source="https://a.com"))
        sources = ec.sources()
        self.assertEqual(sources, ["https://a.com", "https://b.com"])


class ResearchEvidenceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_builds_evidence_from_urls_in_report(self) -> None:
        from domain_agents.research.utils import build_evidence_and_citations

        evidence, citations = build_evidence_and_citations(
            "test_task",
            "Report with https://example.com/paper1 and https://example.com/paper2 as sources.",
        )
        self.assertEqual(len(evidence.items), 2)
        self.assertEqual(len(citations.all()), 2)
        self.assertIn("https://example.com/paper1", evidence.sources())

    async def test_research_builds_evidence_from_worker_results(self) -> None:
        from domain_agents.research.utils import build_evidence_and_citations

        workers = [
            {"subtask_id": "s1", "topic": "ML", "findings": "Found at https://arxiv.org/abs/1234"},
            {"subtask_id": "s2", "topic": "NLP", "findings": "No URLs here"},
        ]
        evidence, citations = build_evidence_and_citations("test_task", "final text", workers)
        url_evidence = evidence.by_type("url")
        self.assertTrue(any("arxiv" in e.source for e in url_evidence))
        self.assertTrue(any(c.url == "https://arxiv.org/abs/1234" for c in citations.all()))
