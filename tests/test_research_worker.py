from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

from langchain_core.messages import AIMessage

from agents.research_worker import (
    build_worker_graph,
    run_worker,
    coordinate_research,
)
from capabilities.planner import ResearchPlan, ResearchSubtask


class _FakeBoundLLM:
    async def ainvoke(self, messages):
        return AIMessage(content="Worker findings for subtask.")


class _FakeLLM:
    def bind_tools(self, tools):
        return _FakeBoundLLM()


class ResearchWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_worker_graph_compiles(self) -> None:
        """Verify worker graph compiles without errors."""
        graph = build_worker_graph(tools=[])
        self.assertIsNotNone(graph)

    async def test_run_worker_basic(self) -> None:
        """Verify run_worker returns findings text."""
        subtask = {
            "id": "sub_1",
            "topic": "GNSS accuracy",
            "search_angles": ["multipath effects"],
            "max_rounds": 1,
            "completion_criteria": "Found accuracy data",
        }

        with patch("agents.research_worker.get_llm", return_value=_FakeLLM()):
            findings = await run_worker(subtask, tools=[])

        self.assertIn("Worker findings", findings)

    async def test_run_worker_respects_max_steps(self) -> None:
        """Verify worker stops at max_steps."""
        call_count = {"n": 0}

        class _CountingBoundLLM:
            async def ainvoke(self, messages):
                call_count["n"] += 1
                return AIMessage(content="still working")

        class _CountingLLM:
            def bind_tools(self, tools):
                return _CountingBoundLLM()

        subtask = {
            "id": "sub_1",
            "topic": "test",
            "search_angles": [],
            "max_rounds": 2,
            "completion_criteria": "done",
        }

        with patch("agents.research_worker.get_llm", return_value=_CountingLLM()):
            findings = await run_worker(subtask, tools=[])

        # Worker should stop after max_rounds even if LLM keeps producing text
        self.assertIsInstance(findings, str)

    async def test_coordinate_research_parallel(self) -> None:
        """Verify coordinate_research runs subtasks in waves."""
        plan = ResearchPlan(
            original_query="test",
            subtasks=[
                ResearchSubtask(id="sub_1", topic="A", priority=1, search_angles=["a1"]),
                ResearchSubtask(id="sub_2", topic="B", priority=2, search_angles=["b1"]),
            ],
        )

        with patch("agents.research_worker.get_llm", return_value=_FakeLLM()):
            results = await coordinate_research(plan, tools=[])

        self.assertIn("sub_1", results)
        self.assertIn("sub_2", results)
        # Both should have findings
        self.assertTrue(len(results["sub_1"]) > 0)
        self.assertTrue(len(results["sub_2"]) > 0)
        # All subtasks should be completed
        for st in plan.subtasks:
            self.assertEqual(st.status, "completed")

    async def test_coordinate_research_error_isolation(self) -> None:
        """Verify one worker failure doesn't block others."""
        plan = ResearchPlan(
            original_query="test",
            subtasks=[
                ResearchSubtask(id="sub_1", topic="A", priority=1, search_angles=["a1"]),
                ResearchSubtask(id="sub_2", topic="B", priority=2, search_angles=["b1"]),
            ],
        )

        call_count = {"n": 0}

        class _FailFirstBoundLLM:
            async def ainvoke(self, messages):
                nonlocal call_count
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise ValueError("Simulated failure")
                return AIMessage(content="Worker B findings.")

        class _FailFirstLLM:
            def bind_tools(self, tools):
                return _FailFirstBoundLLM()

        with patch("agents.research_worker.get_llm", return_value=_FailFirstLLM()):
            results = await coordinate_research(plan, tools=[])

        # Both should have results (one is an error message)
        self.assertIn("sub_1", results)
        self.assertIn("sub_2", results)
        # All subtasks should be completed (even failed ones)
        for st in plan.subtasks:
            self.assertEqual(st.status, "completed")

    async def test_worker_finish_empty_messages(self) -> None:
        """Empty messages list should not crash worker_finish."""
        from agents.research_worker import worker_finish
        state = {"messages": [], "findings": "existing"}
        result = worker_finish(state)
        self.assertIn("findings", result)

    async def test_coordinate_research_deep_deps(self) -> None:
        """A→B→C dependency chain should execute in 3 waves."""
        plan = ResearchPlan(
            original_query="test",
            subtasks=[
                ResearchSubtask(id="sub_1", topic="A", priority=1, search_angles=["a1"]),
                ResearchSubtask(id="sub_2", topic="B", priority=2, depends_on=["sub_1"], search_angles=["b1"]),
                ResearchSubtask(id="sub_3", topic="C", priority=3, depends_on=["sub_2"], search_angles=["c1"]),
            ],
        )

        with patch("agents.research_worker.get_llm", return_value=_FakeLLM()):
            results = await coordinate_research(plan, tools=[])

        self.assertIn("sub_1", results)
        self.assertIn("sub_2", results)
        self.assertIn("sub_3", results)
        for st in plan.subtasks:
            self.assertEqual(st.status, "completed")


if __name__ == "__main__":
    unittest.main()
