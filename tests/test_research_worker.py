from __future__ import annotations

import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from domain_agents.research.worker import build_worker_graph, coordinate_modules, run_worker


_LONG_DRAFT = (
    "## Module Draft\n\n"
    "This module consolidates the problem framing, the scope, and the main evidence-backed claims. "
    "It is long enough to pass heuristic validation and cites https://example.com/source for traceability."
)


class _DraftOnlyLLM:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        prompt = "\n".join(str(getattr(message, "content", "")) for message in messages)
        if "收束校验阶段" in prompt:
            return AIMessage(
                content='{"status":"completed","reason":"ok","missing_requirements":[],"blocker_reason":""}'
            )
        return AIMessage(content=_LONG_DRAFT)


class _EmptyLLM:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        return AIMessage(content="")


class ResearchWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_worker_graph_compiles(self) -> None:
        graph = build_worker_graph(tools=[])
        self.assertIsNotNone(graph)

    async def test_run_worker_returns_completed_result(self) -> None:
        module = {
            "module_id": "related_work",
            "title": "相关工作",
            "owner_role": "citation_worker",
            "objective": "梳理文献并补齐引用。",
        }

        with patch("domain_agents.research.worker.get_llm", return_value=_DraftOnlyLLM()):
            result = await run_worker(module, brief={"clarified_goal": "test"}, tools=[])

        self.assertEqual(result["module_id"], "related_work")
        self.assertEqual(result["status"], "completed")
        self.assertIn("Module Draft", result["findings"])
        self.assertTrue(result["evidence"])

    async def test_coordinate_modules_respects_dependencies(self) -> None:
        plan = {
            "modules": [
                {
                    "module_id": "problem_definition",
                    "title": "问题定义",
                    "owner_role": "argument_worker",
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

        with patch("domain_agents.research.worker.get_llm", return_value=_DraftOnlyLLM()):
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

    async def test_coordinate_modules_marks_blocked_and_skips_dependents(self) -> None:
        plan = {
            "modules": [
                {
                    "module_id": "problem_definition",
                    "title": "问题定义",
                    "owner_role": "argument_worker",
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
                    "module_id": "limitations",
                    "title": "局限性",
                    "owner_role": "argument_worker",
                    "objective": "说明局限性",
                    "depends_on": ["related_work"],
                },
            ]
        }

        with patch("domain_agents.research.worker.get_llm", return_value=_EmptyLLM()):
            result = await coordinate_modules(
                plan=plan,
                brief={"clarified_goal": "test"},
                module_outputs={},
                module_status={"problem_definition": "pending", "related_work": "pending", "limitations": "pending"},
                revision_targets=[],
                tools=[],
            )

        self.assertEqual(result["module_status"]["problem_definition"], "blocked")
        self.assertEqual(result["module_status"]["related_work"], "skipped")
        self.assertEqual(result["module_status"]["limitations"], "skipped")
        self.assertTrue(result["blocked_modules"])
