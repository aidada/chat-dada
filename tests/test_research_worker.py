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

        self.assertIn(result["module_status"]["problem_definition"], {"needs_revision", "blocked"})
        self.assertNotEqual(result["module_status"]["related_work"], "completed")
        self.assertNotEqual(result["module_status"]["limitations"], "completed")
        self.assertEqual(result["module_outputs"]["problem_definition"]["status"], "blocked")

    async def test_coordinate_modules_allows_partial_argument_modules_when_upstream_blocked(self) -> None:
        plan = {
            "modules": [
                {
                    "module_id": "related_work",
                    "title": "相关工作",
                    "owner_role": "citation_worker",
                    "objective": "梳理文献",
                    "depends_on": [],
                },
                {
                    "module_id": "argument_map",
                    "title": "论证链",
                    "owner_role": "argument_worker",
                    "objective": "组织论证",
                    "depends_on": ["related_work"],
                },
                {
                    "module_id": "limitations",
                    "title": "局限性",
                    "owner_role": "argument_worker",
                    "objective": "说明局限性",
                    "depends_on": ["argument_map"],
                },
            ]
        }

        async def _fake_run_worker(module_dict, **kwargs):
            module_id = module_dict["module_id"]
            return {
                "module_id": module_id,
                "status": "partial",
                "findings": f"## {module_dict['title']}\n\nPartial draft based on available context.",
                "evidence": [],
                "blocker_reason": "",
                "search_stats": {"search_rounds": 0, "search_round_delta": 0, "new_evidence_total": 0},
                "worker_state": {"evidence_pack": [], "search_history": [], "query_fingerprints": {}, "search_round": 0, "last_search_metrics": {}},
            }

        with patch("domain_agents.research.worker.run_worker", side_effect=_fake_run_worker):
            result = await coordinate_modules(
                plan=plan,
                brief={"clarified_goal": "test"},
                module_outputs={
                    "related_work": {
                        "module_id": "related_work",
                        "status": "blocked",
                        "content": "## Related Work\n\nCurrent evidence suggests partial coverage with https://example.com/paper",
                        "open_gaps": ["达到检索预算后仍需要更多证据，模块已阻塞。"],
                    }
                },
                module_status={
                    "related_work": "blocked",
                    "argument_map": "pending",
                    "limitations": "pending",
                },
                revision_targets=[],
                tools=[],
            )

        self.assertEqual(result["module_status"]["related_work"], "blocked")
        self.assertEqual(result["module_status"]["argument_map"], "completed")
        self.assertEqual(result["module_status"]["limitations"], "completed")
        self.assertEqual(result["module_outputs"]["argument_map"]["status"], "partial")
        self.assertTrue(result["module_outputs"]["argument_map"]["open_gaps"])
        self.assertIn("以下上游模块尚未完成", result["module_outputs"]["argument_map"]["open_gaps"][0])

    async def test_coordinate_modules_runs_final_partial_pass_after_last_wave_block(self) -> None:
        plan = {
            "modules": [
                {
                    "module_id": "related_work",
                    "title": "相关工作",
                    "owner_role": "citation_worker",
                    "objective": "梳理文献",
                    "depends_on": [],
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

        async def _fake_run_worker(module_dict, **kwargs):
            module_id = module_dict["module_id"]
            if module_id == "related_work":
                return {
                    "module_id": module_id,
                    "status": "blocked",
                    "findings": "## Related Work\n\nCurrent evidence suggests partial coverage.",
                    "evidence": [{"evidence_id": "ev_2", "url": "https://example.com/paper"}],
                    "blocker_reason": "达到检索预算后仍需要更多证据，模块已阻塞。",
                    "search_stats": {"search_rounds": 4, "search_round_delta": 0, "new_evidence_total": 0},
                    "worker_state": {"evidence_pack": [{"evidence_id": "ev_2", "url": "https://example.com/paper"}], "search_history": [], "query_fingerprints": {}, "search_round": 4, "last_search_metrics": {}},
                }
            return {
                "module_id": module_id,
                "status": "partial",
                "findings": "## 论证链\n\nPartial draft after final blocked wave.",
                "evidence": [],
                "blocker_reason": "",
                "search_stats": {"search_rounds": 0, "search_round_delta": 0, "new_evidence_total": 0},
                "worker_state": {"evidence_pack": [], "search_history": [], "query_fingerprints": {}, "search_round": 0, "last_search_metrics": {}},
            }

        with patch("domain_agents.research.worker.run_worker", side_effect=_fake_run_worker):
            result = await coordinate_modules(
                plan=plan,
                brief={"clarified_goal": "test"},
                module_outputs={
                    "related_work": {
                        "module_id": "related_work",
                        "status": "blocked",
                        "content": "## Related Work\n\nCurrent evidence suggests partial coverage.",
                        "open_gaps": ["达到检索预算后仍需要更多证据，模块已阻塞。"],
                    }
                },
                module_status={"related_work": "blocked", "argument_map": "pending"},
                revision_targets=[],
                tools=[],
                existing_evidence_bank=[{"evidence_id": "ev_2", "url": "https://example.com/paper"}],
            )

        self.assertEqual(result["module_status"]["related_work"], "blocked")
        self.assertEqual(result["module_outputs"]["related_work"]["status"], "blocked")
        self.assertEqual(result["module_status"]["argument_map"], "completed")
        self.assertEqual(result["module_outputs"]["argument_map"]["status"], "partial")
