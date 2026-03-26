from __future__ import annotations

import unittest
from unittest.mock import patch

from capabilities.review_gates import ReviewResult
from domain_agents.research.config import (
    ACADEMIC_PAPER_GUIDANCE_PROFILE,
    DEFAULT_DELIVERABLE_TYPE,
    resolve_deliverable_type,
    resolve_report_profile,
)
from domain_agents.research.reviewers import ResearchReviewGate
from domain_agents.research.workflow import build_research_workflow_graph, checkpoint_b_node


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
