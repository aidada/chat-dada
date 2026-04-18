from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.workflows.research.workflow import evaluate_draft_node


class ResearchReviewDiffTests(unittest.IsolatedAsyncioTestCase):
    async def test_evaluate_draft_emits_structured_diff(self) -> None:
        review = SimpleNamespace(
            passed=False,
            needs_replan=False,
            summary="第二轮提升了 related_work，但 problem_definition 仍需补强。",
            dimensions=[
                SimpleNamespace(
                    name="citation_relevance_coverage",
                    score=0.8,
                    passed=True,
                    strengths=["coverage improved"],
                    weaknesses=[],
                    affected_modules=["related_work"],
                    metadata={},
                ),
                SimpleNamespace(
                    name="intent_alignment",
                    score=0.4,
                    passed=False,
                    strengths=[],
                    weaknesses=["problem definition still vague"],
                    affected_modules=["problem_definition"],
                    metadata={},
                ),
            ],
            revision_targets=[
                SimpleNamespace(
                    module_id="problem_definition",
                    reason="still vague",
                    priority="high",
                    actions=["clarify scope"],
                    preserve_constraints=[],
                    requires_new_evidence=False,
                    metadata={},
                )
            ],
            lock_modules=["related_work"],
            user_feedback_required=False,
            issues=[],
        )

        state = {
            "brief": {"clarified_goal": "test", "deliverable_type": "literature_review"},
            "plan": {"modules": [{"module_id": "problem_definition"}, {"module_id": "related_work"}]},
            "aggregated_draft": "## Draft\n\ncurrent text",
            "module_outputs": {
                "problem_definition": {"content": "old"},
                "related_work": {"content": "newer"},
            },
            "module_status": {
                "problem_definition": "completed",
                "related_work": "completed",
            },
            "evaluations": [
                {
                    "summary": "第一轮",
                    "dimensions": [
                        {"name": "citation_relevance_coverage", "score": 0.5},
                        {"name": "intent_alignment", "score": 0.6},
                    ],
                    "revision_targets": [
                        {"module_id": "problem_definition"},
                        {"module_id": "related_work"},
                    ],
                }
            ],
            "locked_modules": {},
            "evidence_bank": [],
            "citation_bank": [],
            "blocked_modules": [],
            "workflow_trace": [],
        }

        with patch("agent.workflows.research.workflow.ResearchReviewGate.evaluate", return_value=review):
            result = await evaluate_draft_node(state)

        diff = result["last_evaluation_diff"]
        self.assertIn("related_work", diff["resolved_modules"])
        self.assertIn("problem_definition", diff["unchanged_modules"])
        self.assertTrue(any(item["name"] == "citation_relevance_coverage" for item in diff["dimension_changes"]))

    async def test_evaluate_draft_keeps_blocked_targets_blocked(self) -> None:
        review = SimpleNamespace(
            passed=False,
            needs_replan=False,
            summary="blocked target still needs evidence",
            dimensions=[],
            revision_targets=[
                SimpleNamespace(
                    module_id="related_work",
                    reason="needs more evidence",
                    priority="high",
                    actions=["collect evidence"],
                    preserve_constraints=[],
                    requires_new_evidence=True,
                    metadata={},
                )
            ],
            lock_modules=[],
            user_feedback_required=False,
            issues=[],
        )

        state = {
            "brief": {"clarified_goal": "test", "deliverable_type": "literature_review"},
            "plan": {"modules": [{"module_id": "related_work"}]},
            "aggregated_draft": "## Draft\n\ncurrent text",
            "module_outputs": {
                "related_work": {"content": "blocked draft", "locked": False},
            },
            "module_status": {
                "related_work": "blocked",
            },
            "evaluations": [],
            "locked_modules": {},
            "evidence_bank": [],
            "citation_bank": [],
            "blocked_modules": [{"module_id": "related_work", "reason": "budget exhausted"}],
            "workflow_trace": [],
        }

        with patch("agent.workflows.research.workflow.ResearchReviewGate.evaluate", return_value=review):
            result = await evaluate_draft_node(state)

        self.assertEqual(result["module_status"]["related_work"], "blocked")
