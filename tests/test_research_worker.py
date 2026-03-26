from __future__ import annotations

import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from domain_agents.research.worker import (
    build_worker_graph,
    coordinate_modules,
    run_worker,
    worker_should_continue,
)


class _FakeBoundLLM:
    async def ainvoke(self, messages):
        return AIMessage(content="## Module Draft\n\nWorker findings with https://example.com/source")


class _FakeLLM:
    def bind_tools(self, tools):
        return _FakeBoundLLM()


class _EmptyBoundLLM:
    async def ainvoke(self, messages):
        return AIMessage(content="")


class _EmptyLLM:
    def bind_tools(self, tools):
        return _EmptyBoundLLM()


class ResearchWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_worker_graph_compiles(self) -> None:
        graph = build_worker_graph(tools=[])
        self.assertIsNotNone(graph)

    def test_worker_should_continue_allows_final_tool_turn(self) -> None:
        state = {
            "step_count": 3,
            "max_steps": 3,
            "messages": [AIMessage(content="", tool_calls=[{"name": "exa_deep_search", "args": {}, "id": "call_1"}])],
        }
        self.assertEqual(worker_should_continue(state), "tools")

    async def test_run_worker_returns_structured_result(self) -> None:
        module = {
            "module_id": "related_work",
            "title": "相关工作",
            "owner_role": "citation_worker",
            "objective": "梳理文献并补齐引用。",
        }

        with patch("domain_agents.research.worker.get_llm", return_value=_FakeLLM()):
            result = await run_worker(module, brief={"clarified_goal": "test"}, tools=[])

        self.assertEqual(result["module_id"], "related_work")
        self.assertEqual(result["status"], "ok")
        self.assertIn("Module Draft", result["findings"])
        self.assertTrue(result["evidence"])

    async def test_coordinate_modules_respects_dependencies(self) -> None:
        plan = {
            "modules": [
                {
                    "module_id": "problem_definition",
                    "title": "问题定义",
                    "owner_role": "citation_worker",
                    "objective": "定义问题",
                    "depends_on": [],
                },
                {
                    "module_id": "related_work",
                    "title": "相关工作",
                    "owner_role": "citation_worker",
                    "objective": "梳理文献",
                    "depends_on": ["problem_definition"],
                },
                {
                    "module_id": "argument_map",
                    "title": "论证链",
                    "owner_role": "argument_worker",
                    "objective": "组织论证",
                    "depends_on": ["related_work"],
                },
            ]
        }
        module_status = {module["module_id"]: "pending" for module in plan["modules"]}

        with patch("domain_agents.research.worker.get_llm", return_value=_FakeLLM()):
            result = await coordinate_modules(
                plan=plan,
                brief={"clarified_goal": "test"},
                module_outputs={},
                module_status=module_status,
                revision_targets=[],
                tools=[],
            )

        self.assertEqual(result["module_status"]["problem_definition"], "completed")
        self.assertEqual(result["module_status"]["related_work"], "completed")
        self.assertEqual(result["module_status"]["argument_map"], "completed")
        self.assertIn("argument_map", result["module_outputs"])

    async def test_coordinate_modules_keeps_empty_worker_outputs_in_revision(self) -> None:
        plan = {
            "modules": [
                {
                    "module_id": "problem_definition",
                    "title": "问题定义",
                    "owner_role": "citation_worker",
                    "objective": "定义问题",
                    "depends_on": [],
                },
            ]
        }
        module_status = {"problem_definition": "pending"}

        with patch("domain_agents.research.worker.get_llm", return_value=_EmptyLLM()):
            result = await coordinate_modules(
                plan=plan,
                brief={"clarified_goal": "test"},
                module_outputs={},
                module_status=module_status,
                revision_targets=[],
                tools=[],
            )

        self.assertEqual(result["module_status"]["problem_definition"], "needs_revision")
        self.assertNotIn("problem_definition", result["module_outputs"])
