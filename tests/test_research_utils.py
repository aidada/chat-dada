from __future__ import annotations

import unittest

from agent.workflows.research.utils import fallback_brief, fallback_plan, feedback_action, merge_brief


class MergeBriefTests(unittest.TestCase):
    def test_feedback_action_treats_no_change_phrases_as_accept(self) -> None:
        self.assertEqual(feedback_action("无修改"), "accept")
        self.assertEqual(feedback_action("没有修改 继续"), "accept")
        self.assertEqual(feedback_action("按原计划继续"), "accept")

    def test_merge_brief_normalizes_string_list_fields(self) -> None:
        base = fallback_brief("帮我写一篇论文", None, {})
        merged = merge_brief(
            base,
            {
                "literature_languages": "未指定",
                "preferred_emphasis": "recent literature, engineering feasibility",
                "user_constraints": "控制在 3000 字以内",
                "success_criteria": ["引用可追溯", "未指定"],
                "unresolved_questions": "",
            },
            {},
        )

        self.assertEqual(merged["literature_languages"], ["en", "zh"])
        self.assertEqual(
            merged["preferred_emphasis"],
            ["recent literature", "engineering feasibility"],
        )
        self.assertEqual(merged["user_constraints"], ["控制在 3000 字以内"])
        self.assertEqual(merged["success_criteria"], ["引用可追溯"])
        self.assertEqual(merged["unresolved_questions"], [])

    def test_merge_brief_coerces_structured_scalar_fields(self) -> None:
        base = fallback_brief("分析 GNSS 数据", None, {})
        merged = merge_brief(
            base,
            {
                "discipline": ["GNSS/卫星导航", "智能网联车辆定位"],
                "research_mode": ["实证研究", "方法验证"],
                "time_scope": {
                    "dataset_period": "2025-08-11 to 2025-08-30",
                    "review_period": "未指定",
                },
            },
            {},
        )

        self.assertEqual(merged["discipline"], "GNSS/卫星导航 / 智能网联车辆定位")
        self.assertEqual(merged["research_mode"], "实证研究 / 方法验证")
        self.assertEqual(
            merged["time_scope"],
            "dataset_period: 2025-08-11 to 2025-08-30; review_period: 未指定",
        )

    def test_merge_brief_preserves_unresolved_question_sentence(self) -> None:
        base = fallback_brief("分析 GNSS 数据", None, {})
        merged = merge_brief(
            base,
            {
                "unresolved_questions": "原始GNSS数据中除伪距外，是否还包含载波相位、多普勒或 SNR？",
            },
            {},
        )

        self.assertEqual(
            merged["unresolved_questions"],
            ["原始GNSS数据中除伪距外，是否还包含载波相位、多普勒或 SNR？"],
        )

    def test_fallback_brief_includes_clarification_history_as_constraints(self) -> None:
        brief = fallback_brief(
            "分析 GNSS 数据",
            None,
            {
                "clarification_history": [
                    {"question": "论文目标是什么？", "answer": "英文 SCI 期刊"},
                ]
            },
        )

        self.assertIn("论文目标是什么？ -> 英文 SCI 期刊", brief["user_constraints"])

    def test_comparison_query_fallback_plan_has_parallel_research_directions(self) -> None:
        brief = fallback_brief("调研 AWS S3 和阿里云 OSS 的定价对比", None, {})
        plan = fallback_plan(brief)

        first_wave = [module for module in plan["modules"] if not module.get("depends_on")]
        self.assertGreaterEqual(len(first_wave), 3)
        objectives = "\n".join(str(module.get("objective", "")) for module in first_wave)
        self.assertIn("AWS S3", objectives)
        self.assertIn("阿里云 OSS", objectives)
        self.assertTrue(
            any(module["module_id"] == "comparative_matrix" for module in plan["modules"]),
            "orchestrator-facing comparison synthesis module should be explicit",
        )


if __name__ == "__main__":
    unittest.main()
