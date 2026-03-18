from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from capabilities.planner import (
    ResearchPlan,
    ResearchSubtask,
    _parse_plan_json,
    generate_research_plan,
    get_next_subtask,
    is_plan_complete,
)


class ResearchPlannerTests(unittest.IsolatedAsyncioTestCase):
    def test_subtask_round_trip(self) -> None:
        st = ResearchSubtask(
            id="sub_1", topic="GNSS accuracy", search_angles=["multipath", "NLOS"],
            depends_on=[], priority=1, max_rounds=3, status="pending",
            completion_criteria="Found accuracy numbers",
        )
        restored = ResearchSubtask.from_dict(st.to_dict())
        self.assertEqual(restored.id, "sub_1")
        self.assertEqual(restored.topic, "GNSS accuracy")
        self.assertEqual(restored.search_angles, ["multipath", "NLOS"])
        self.assertEqual(restored.priority, 1)

    def test_plan_round_trip(self) -> None:
        plan = ResearchPlan(
            original_query="GNSS NLOS",
            clarified_goal="Detect NLOS signals",
            subtasks=[
                ResearchSubtask(id="sub_1", topic="GNSS basics", priority=1),
                ResearchSubtask(id="sub_2", topic="NLOS detection", depends_on=["sub_1"], priority=2),
            ],
            global_constraints=["Focus on urban environments"],
        )
        restored = ResearchPlan.from_dict(plan.to_dict())
        self.assertEqual(restored.original_query, "GNSS NLOS")
        self.assertEqual(len(restored.subtasks), 2)
        self.assertEqual(restored.subtasks[1].depends_on, ["sub_1"])
        self.assertEqual(restored.global_constraints, ["Focus on urban environments"])

    def test_get_next_subtask_respects_deps(self) -> None:
        plan = ResearchPlan(subtasks=[
            ResearchSubtask(id="sub_1", topic="A", priority=2, status="pending"),
            ResearchSubtask(id="sub_2", topic="B", depends_on=["sub_1"], priority=1, status="pending"),
        ])
        # sub_2 has higher priority but depends on sub_1
        next_st = get_next_subtask(plan)
        self.assertEqual(next_st.id, "sub_1")

    def test_get_next_subtask_priority(self) -> None:
        plan = ResearchPlan(subtasks=[
            ResearchSubtask(id="sub_1", topic="A", priority=2, status="pending"),
            ResearchSubtask(id="sub_2", topic="B", priority=1, status="pending"),
        ])
        # Both have no deps, sub_2 has higher priority
        next_st = get_next_subtask(plan)
        self.assertEqual(next_st.id, "sub_2")

    def test_get_next_subtask_all_done(self) -> None:
        plan = ResearchPlan(subtasks=[
            ResearchSubtask(id="sub_1", status="completed"),
            ResearchSubtask(id="sub_2", status="skipped"),
        ])
        self.assertIsNone(get_next_subtask(plan))

    def test_is_plan_complete(self) -> None:
        plan = ResearchPlan(subtasks=[
            ResearchSubtask(id="sub_1", status="completed"),
            ResearchSubtask(id="sub_2", status="skipped"),
        ])
        self.assertTrue(is_plan_complete(plan))

        plan.subtasks.append(ResearchSubtask(id="sub_3", status="pending"))
        self.assertFalse(is_plan_complete(plan))

    async def test_generate_plan_parses_output(self) -> None:
        plan_json = json.dumps({
            "clarified_goal": "Understand GNSS NLOS detection",
            "subtasks": [
                {"id": "sub_1", "topic": "GNSS basics", "search_angles": ["GNSS accuracy"], "depends_on": [], "priority": 1, "max_rounds": 3, "completion_criteria": "Found basics"},
                {"id": "sub_2", "topic": "NLOS detection", "search_angles": ["NLOS methods"], "depends_on": ["sub_1"], "priority": 2, "max_rounds": 3, "completion_criteria": "Found methods"},
            ],
            "global_constraints": ["Urban focus"],
        })

        class _MockLLM:
            async def ainvoke(self, messages):
                return AIMessage(content=plan_json)

        with patch("capabilities.planner.get_llm", return_value=_MockLLM()):
            plan = await generate_research_plan("GNSS NLOS detection")

        self.assertEqual(plan.clarified_goal, "Understand GNSS NLOS detection")
        self.assertEqual(len(plan.subtasks), 2)
        self.assertEqual(plan.subtasks[0].id, "sub_1")
        self.assertEqual(plan.subtasks[1].depends_on, ["sub_1"])

    def test_parse_plan_json_code_block(self) -> None:
        text = """Here is the plan:
```json
{"clarified_goal": "test", "subtasks": []}
```
Done."""
        result = _parse_plan_json(text)
        self.assertEqual(result["clarified_goal"], "test")

    def test_parse_plan_json_plain(self) -> None:
        text = '{"clarified_goal": "test", "subtasks": []}'
        result = _parse_plan_json(text)
        self.assertEqual(result["clarified_goal"], "test")

    async def test_generate_plan_empty_query_raises(self) -> None:
        with self.assertRaises(ValueError):
            await generate_research_plan("")

    def test_plan_to_dict_includes_version(self) -> None:
        from research_planner import PLAN_VERSION
        plan = ResearchPlan(original_query="test")
        data = plan.to_dict()
        self.assertEqual(data["_version"], PLAN_VERSION)

    def test_subtask_to_dict_includes_version(self) -> None:
        from research_planner import SUBTASK_VERSION
        st = ResearchSubtask(id="sub_1", topic="test")
        data = st.to_dict()
        self.assertEqual(data["_version"], SUBTASK_VERSION)

    async def test_generate_plan_fallback_on_invalid_json(self) -> None:
        class _BadJsonLLM:
            async def ainvoke(self, messages):
                return AIMessage(content="This is not valid JSON at all")

        with patch("capabilities.planner.get_llm", return_value=_BadJsonLLM()):
            plan = await generate_research_plan("GNSS test")

        # Should fallback to single-subtask plan
        self.assertEqual(len(plan.subtasks), 1)
        self.assertEqual(plan.subtasks[0].id, "sub_1")

    def test_get_next_subtask_circular_deps(self) -> None:
        plan = ResearchPlan(subtasks=[
            ResearchSubtask(id="sub_1", topic="A", depends_on=["sub_2"], status="pending"),
            ResearchSubtask(id="sub_2", topic="B", depends_on=["sub_1"], status="pending"),
        ])
        # Neither can start due to circular dependency
        self.assertIsNone(get_next_subtask(plan))

    def test_is_plan_complete_empty_subtasks(self) -> None:
        plan = ResearchPlan(subtasks=[])
        self.assertTrue(is_plan_complete(plan))


if __name__ == "__main__":
    unittest.main()
