from __future__ import annotations

import pytest
from unittest.mock import patch

from agent.workflows.office.goal_contract import (
    GoalNormalizationRequest,
    NeedClarification,
    NormalizeOk,
    RejectNormalization,
)
from agent.workflows.office.goal_normalizer import infer_requested_slide_count, normalize_goal_profile


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("创建一份约 10 页的 PPT，主题：钓鱼对中青年男性的好处。", 10),
        ("做一个10页的PPT，主题是钓鱼的好处。", 10),
        ("请帮我做个 8 页的演示文稿。", 8),
    ],
)
def test_infer_requested_slide_count_accepts_chinese_page_phrasing(
    goal: str,
    expected: int,
) -> None:
    assert infer_requested_slide_count(goal) == expected


class _FakeStructuredLLM:
    def __init__(self, schema, payload):
        self._schema = schema
        self._payload = payload

    async def ainvoke(self, _messages, **_kwargs):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._schema(**self._payload)


class _FakeLLM:
    def __init__(self, payload):
        self._payload = payload

    def with_structured_output(self, schema):
        return _FakeStructuredLLM(schema, self._payload)


@pytest.mark.asyncio
async def test_normalize_goal_profile_prefers_raw_user_message_over_summary_conflict() -> None:
    fake_llm = _FakeLLM(
        {
            "format": "pptx",
            "operation": "create",
            "requested_slide_count": 6,
            "output_filename": "summary-deck.pptx",
            "quality_profile": {"animations": False, "visuals": False, "notes": True},
            "confidence": "medium",
            "missing_fields": [],
        }
    )

    with patch("agent.workflows.office.goal_normalizer.get_llm", return_value=fake_llm):
        result = await normalize_goal_profile(
            GoalNormalizationRequest(
                raw_user_message="请做一个大概 10 页的 PPT，介绍 chat-dada 的能力。",
                orchestrator_summary="Create a 6-slide deck introducing chat-dada.",
                file_hint="",
                source_files=[],
                reference_files=[],
                explicit_format=None,
                explicit_operation=None,
                clarification_history=[],
            )
        )

    assert isinstance(result, NormalizeOk)
    assert result.profile.requested_slide_count == 10


@pytest.mark.asyncio
async def test_normalize_goal_profile_returns_none_when_slide_count_unspecified() -> None:
    fake_llm = _FakeLLM(
        {
            "format": "pptx",
            "operation": "create",
            "requested_slide_count": None,
            "output_filename": "chat-dada-intro.pptx",
            "quality_profile": {"animations": False, "visuals": True, "notes": True},
            "confidence": "medium",
            "missing_fields": [],
        }
    )

    with patch("agent.workflows.office.goal_normalizer.get_llm", return_value=fake_llm):
        result = await normalize_goal_profile(
            GoalNormalizationRequest(
                raw_user_message="帮我做一个介绍 chat-dada 能做什么的 PPT。",
                orchestrator_summary="Create a presentation introducing chat-dada.",
                file_hint="",
                source_files=[],
                reference_files=[],
                explicit_format=None,
                explicit_operation=None,
                clarification_history=[],
            )
        )

    assert isinstance(result, NormalizeOk)
    assert result.profile.requested_slide_count is None


@pytest.mark.asyncio
async def test_normalize_goal_profile_requests_clarification_for_ambiguous_slide_intent() -> None:
    fake_llm = _FakeLLM(
        {
            "format": "pptx",
            "operation": "create",
            "requested_slide_count": None,
            "output_filename": "deck.pptx",
            "quality_profile": {"animations": False, "visuals": False, "notes": True},
            "confidence": "low",
            "missing_fields": ["requested_slide_count"],
        }
    )

    with patch("agent.workflows.office.goal_normalizer.get_llm", return_value=fake_llm):
        result = await normalize_goal_profile(
            GoalNormalizationRequest(
                raw_user_message="帮我做个十来页左右的 PPT，介绍 chat-dada。",
                orchestrator_summary="Create a PPT about chat-dada.",
                file_hint="",
                source_files=[],
                reference_files=[],
                explicit_format=None,
                explicit_operation=None,
                clarification_history=[],
            )
        )

    assert isinstance(result, NeedClarification)
    assert result.missing_fields == ["requested_slide_count"]
    assert any("多少页" in question for question in result.questions)


@pytest.mark.asyncio
async def test_normalize_goal_profile_rejects_non_office_requests() -> None:
    fake_llm = _FakeLLM(
        {
            "format": None,
            "operation": None,
            "requested_slide_count": None,
            "output_filename": None,
            "quality_profile": {"animations": False, "visuals": False, "notes": False},
            "confidence": "low",
            "missing_fields": ["format", "operation"],
        }
    )

    with patch("agent.workflows.office.goal_normalizer.get_llm", return_value=fake_llm):
        result = await normalize_goal_profile(
            GoalNormalizationRequest(
                raw_user_message="帮我查一下 OpenAI 最新模型有哪些。",
                orchestrator_summary="Research the latest OpenAI models.",
                file_hint="",
                source_files=[],
                reference_files=[],
                explicit_format=None,
                explicit_operation=None,
                clarification_history=[],
            )
        )

    assert isinstance(result, RejectNormalization)
