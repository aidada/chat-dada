from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from capabilities.review_gates import ReviewResult
from capabilities.memory import ResearchMemory
from domain_agents.research.config import (
    ACADEMIC_PAPER_GUIDANCE_PROFILE,
    DEFAULT_DELIVERABLE_TYPE,
    resolve_deliverable_type,
    resolve_report_profile,
)
from domain_agents.research.reviewers import ResearchReviewGate
from domain_agents.research.workflow import (
    _actionable_revision_targets,
    _should_retry_workflow_llm_node,
    aggregate_draft_node,
    build_research_workflow_graph,
    checkpoint_a_node,
    checkpoint_b_node,
    planner_node,
    synthesize_final_node,
)


class ResearchWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_report_profile_auto_selects_academic(self) -> None:
        profile = resolve_report_profile(
            "请做文献综述，并说明这篇论文后续应该怎么写 introduction 和 experiment",
        )
        self.assertEqual(profile, ACADEMIC_PAPER_GUIDANCE_PROFILE)

    def test_resolve_deliverable_type_defaults_to_literature_review(self) -> None:
        deliverable = resolve_deliverable_type("请分析这个技术方向的发展趋势")
        self.assertEqual(deliverable, DEFAULT_DELIVERABLE_TYPE)

    def test_build_research_workflow_graph_compiles(self) -> None:
        graph = build_research_workflow_graph()
        self.assertIsNotNone(graph)
        for node_name in ("intake", "planner", "aggregate_draft", "optimize_modules", "synthesize_final"):
            self.assertIsNotNone(graph.builder.nodes[node_name].retry_policy)

    def test_actionable_revision_targets_preserves_non_terminal_revision_targets(self) -> None:
        targets = [
            {"module_id": "related_work"},
            {"module_id": "argument_map"},
            {"module_id": "limitations"},
        ]
        statuses = {
            "related_work": "blocked",
            "argument_map": "skipped",
            "limitations": "needs_revision",
        }
        actionable = _actionable_revision_targets(targets, statuses)
        self.assertEqual([item["module_id"] for item in actionable], ["related_work", "argument_map", "limitations"])

    def test_workflow_llm_retry_policy_matches_transient_gateway_errors(self) -> None:
        self.assertTrue(_should_retry_workflow_llm_node(httpx.RemoteProtocolError("incomplete chunked read")))
        self.assertTrue(_should_retry_workflow_llm_node(RuntimeError("502 Bad Gateway")))
        self.assertFalse(_should_retry_workflow_llm_node(ValueError("invalid planner json")))

    async def test_research_review_gate_emits_revision_targets(self) -> None:
        gate = ResearchReviewGate()
        review: ReviewResult = await gate.evaluate(
            {
                "brief": {
                    "deliverable_type": "paper_guidance",
                    "clarified_goal": "为论文写作准备研究草案",
                },
                "plan": {
                    "modules": [
                        {"module_id": "problem_definition"},
                        {"module_id": "related_work"},
                        {"module_id": "method_candidates"},
                        {"module_id": "experiment_design"},
                        {"module_id": "argument_map"},
                        {"module_id": "contributions"},
                        {"module_id": "limitations"},
                    ]
                },
                "report": "## 文献综述正文\n\n只有很短的草案。",
                "module_outputs": {
                    "problem_definition": {"content": "问题定义"},
                    "related_work": {"content": "没有引用的 related work"},
                },
                "evidence_bank": [],
            }
        )

        self.assertFalse(review.passed)
        self.assertTrue(review.revision_targets)
        self.assertTrue(any(target.module_id == "related_work" for target in review.revision_targets))

    async def test_checkpoint_b_preserves_evaluator_replan_signal(self) -> None:
        state = {
            "needs_replan": True,
            "revision_targets": [
                {
                    "module_id": "problem_definition",
                    "reason": "当前草案与用户目标不对齐",
                    "priority": "high",
                    "actions": ["重新校准任务定义"],
                }
            ],
            "evaluations": [
                {
                    "summary": "评审未通过，需要改方向。",
                    "revision_targets": [
                        {
                            "module_id": "problem_definition",
                            "reason": "当前草案与用户目标不对齐",
                            "priority": "high",
                            "actions": ["重新校准任务定义"],
                        }
                    ],
                }
            ],
            "aggregated_draft": "## 草稿\n\n当前仍偏综述。",
            "feedback_history": [],
            "plan": {"modules": [{"module_id": "problem_definition"}]},
            "workflow_trace": [],
        }

        with patch("domain_agents.research.workflow.ask_user", return_value="继续修订"):
            result = await checkpoint_b_node(state)

        self.assertTrue(result["needs_replan"])

    async def test_planner_preserves_budget_context_for_review_driven_replan(self) -> None:
        state = {
            "brief": {"clarified_goal": "重规划"},
            "active_checkpoint": "checkpoint_b",
            "needs_replan": True,
            "aggregated_draft": "## draft",
            "draft_history": [{"draft": "v1", "at": 1}],
            "evaluations": [{"summary": "需要重规划"}],
            "last_evaluation_diff": {"changed_modules": ["related_work"]},
            "budget": {"awaiting_user_decision": False, "soft_budget_total": 6},
            "revision_round": 2,
            "workflow_trace": [],
        }

        with patch("domain_agents.research.workflow._invoke_llm_text", return_value=""):
            result = await planner_node(state)

        self.assertEqual(result["budget"]["soft_budget_total"], 6)
        self.assertEqual(result["revision_round"], 2)
        self.assertEqual(result["aggregated_draft"], "## draft")
        self.assertEqual(result["draft_history"], [{"draft": "v1", "at": 1}])
        self.assertEqual(result["evaluations"], [{"summary": "需要重规划"}])
        self.assertTrue(result["skip_checkpoint_a_once"])

    async def test_checkpoint_a_skips_user_prompt_once_after_review_replan(self) -> None:
        state = {
            "plan": {"modules": [{"module_id": "problem_definition", "title": "研究问题定义"}]},
            "skip_checkpoint_a_once": True,
            "workflow_trace": [],
        }

        with patch("domain_agents.research.workflow.ask_user") as mocked_ask:
            result = await checkpoint_a_node(state)

        mocked_ask.assert_not_called()
        self.assertFalse(result["skip_checkpoint_a_once"])
        self.assertFalse(result["needs_replan"])

    async def test_aggregate_draft_writes_full_draft_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state = {
                "task_id": "task_stage_file",
                "query": "研究任务",
                "report_profile": "",
                "brief": {"clarified_goal": "生成草稿"},
                "module_outputs": {
                    "intro": {"content": "## 引言\n\n完整草稿正文"},
                },
                "module_status": {"intro": "completed"},
                "blocked_modules": [],
                "revision_round": 0,
                "draft_history": [],
                "workflow_trace": [],
            }

            def _memory_factory(task_id: str):
                return ResearchMemory(task_id, root=Path(tmpdir))

            with patch("domain_agents.research.workflow._invoke_llm_text", return_value=""), patch(
                "domain_agents.research.workflow.ResearchMemory",
                side_effect=_memory_factory,
            ):
                result = await aggregate_draft_node(state)

            draft_path = Path(tmpdir) / "task_stage_file" / "aggregated_draft.md"
            self.assertTrue(draft_path.exists())
            self.assertEqual(draft_path.read_text(encoding="utf-8"), result["aggregated_draft"])

    async def test_checkpoint_b_emits_stage_artifacts_for_current_draft(self) -> None:
        state = {
            "task_id": "task_stage_emit",
            "needs_replan": False,
            "revision_targets": [],
            "evaluations": [{"summary": "评审未通过，需要补强论证。", "revision_targets": []}],
            "aggregated_draft": "## 草稿\n\n需要完整查看。",
            "feedback_history": [],
            "plan": {"modules": [{"module_id": "problem_definition"}]},
            "workflow_trace": [],
        }
        emitted: list[tuple[str, object]] = []

        with patch("domain_agents.research.workflow.ask_user", return_value="继续修订"), patch(
            "domain_agents.research.workflow._safe_emit",
            side_effect=lambda event_type, content: emitted.append((event_type, content)),
        ):
            await checkpoint_b_node(state)

        stage_events = [
            payload
            for event_type, payload in emitted
            if event_type == "stage_artifacts" and isinstance(payload, dict)
        ]
        self.assertGreaterEqual(len(stage_events), 2)
        self.assertEqual(stage_events[0]["stage_id"], "checkpoint_b")
        self.assertEqual(stage_events[0]["status"], "ready")
        self.assertEqual(stage_events[0]["files"][0]["name"], "当前研究草稿.md")
        self.assertIn("/tasks/task_stage_emit/artifact-file?path=aggregated_draft.md", stage_events[0]["files"][0]["url"])
        self.assertEqual(stage_events[-1]["status"], "cleared")

    async def test_synthesize_final_writes_markdown_artifact_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            state = {
                "task_id": "task_final_emit",
                "query": "研究任务",
                "report_profile": "",
                "aggregated_draft": "## 草稿\n\n完整中间稿。",
                "module_outputs": {"intro": {"content": "## 引言\n\n正文"}},
                "evaluations": [{"passed": True, "issues": []}],
                "blocked_modules": [],
                "workflow_trace": [],
            }
            emitted: list[tuple[str, object]] = []

            def _memory_factory(task_id: str):
                return ResearchMemory(task_id, root=Path(tmpdir))

            with patch("domain_agents.research.workflow._invoke_llm_text", return_value=""), patch(
                "domain_agents.research.workflow.ResearchMemory",
                side_effect=_memory_factory,
            ), patch(
                "domain_agents.research.workflow._safe_emit",
                side_effect=lambda event_type, content: emitted.append((event_type, content)),
            ):
                result = await synthesize_final_node(state)

            final_path = Path(tmpdir) / "task_final_emit" / "final_report.md"
            self.assertTrue(final_path.exists())
            self.assertEqual(final_path.read_text(encoding="utf-8"), result["final_result"])
            stage_events = [
                payload
                for event_type, payload in emitted
                if event_type == "stage_artifacts" and isinstance(payload, dict)
            ]
            self.assertTrue(stage_events)
            self.assertEqual(stage_events[-1]["stage_id"], "final_report")
            self.assertEqual(stage_events[-1]["files"][0]["name"], "最终研究输出.md")
            self.assertIn("/tasks/task_final_emit/artifact-file?path=final_report.md", stage_events[-1]["files"][0]["url"])

    async def test_checkpoint_b_extends_budget_after_user_confirms_continue(self) -> None:
        state = {
            "task_id": "task_budget_extend",
            "needs_replan": False,
            "revision_targets": [{"module_id": "related_work", "reason": "覆盖不足", "actions": ["补文献"]}],
            "evaluations": [{"summary": "评审未通过，需要补强 related_work。", "revision_targets": []}],
            "aggregated_draft": "## 草稿\n\nrelated work 仍不足。",
            "feedback_history": [],
            "plan": {"modules": [{"module_id": "related_work"}]},
            "module_status": {"related_work": "blocked"},
            "budget": {
                "awaiting_user_decision": True,
                "module_budgets": {
                    "related_work": {
                        "soft_budget": 3,
                        "hard_budget": 5,
                        "consumed_rounds": 5,
                        "terminal_blocked": True,
                    }
                },
            },
            "workflow_trace": [],
        }

        with patch("domain_agents.research.workflow.ask_user", return_value="继续"):
            result = await checkpoint_b_node(state)

        self.assertFalse(result["budget"]["awaiting_user_decision"])
        self.assertEqual(result["budget"]["last_user_decision"], "extend")
        self.assertGreaterEqual(result["budget"]["module_budgets"]["related_work"]["hard_budget"], 7)
