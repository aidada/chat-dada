from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.errors import GraphRecursionError


@pytest.mark.asyncio
async def test_officecli_run_returns_structured_validation_success() -> None:
    from agent.domains.ppt.tools import officecli_run

    with (
        patch(
            "agent.tools.officecli._execute_officecli_raw_via_gateway",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "agent.tools.officecli._run_officecli_raw_locally",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "stdout": "Validation passed: no errors found.",
                    "stderr": "",
                    "exit_status": 0,
                }
            ),
        ),
    ):
        payload = json.loads(await officecli_run.ainvoke({"command": "validate demo.pptx"}))

    assert payload["success"] is True
    assert payload["kind"] == "validation_passed"
    assert payload["command"] == "validate demo.pptx"


def test_ppt_content_researcher_has_real_search_tools() -> None:
    from agent.domains.ppt.workflow import PPT_SUBAGENTS

    [content_researcher] = [agent for agent in PPT_SUBAGENTS if agent.name == "content_researcher"]
    tool_names = {tool.name for tool in content_researcher.tools}

    assert "web_search" in tool_names
    assert "academic_search" in tool_names
    assert "brave_search" in tool_names


def test_ppt_officecli_skill_bundle_includes_pptx_skill() -> None:
    from agent.tools.officecli_skill_loader import build_officecli_skill_bundle

    bundle = build_officecli_skill_bundle("做一个介绍 AI 的 PPT", format_hint="pptx")
    assert "# Extra skill: officecli-pptx" in bundle


@pytest.mark.asyncio
async def test_officecli_run_rejects_non_officecli_command() -> None:
    from agent.domains.ppt.tools import officecli_run

    with (
        patch(
            "agent.tools.officecli._execute_officecli_raw_via_gateway",
            new=AsyncMock(),
        ) as mocked_gateway,
        patch(
            "agent.tools.officecli._run_officecli_raw_locally",
            new=AsyncMock(),
        ) as mocked_local,
    ):
        payload = json.loads(await officecli_run.ainvoke({"command": "python3 /tmp/build_ppt.py"}))

    assert payload["success"] is False
    assert payload["kind"] == "fatal_error"
    assert "Rejected non-OfficeCLI command root" in payload["message"]
    mocked_gateway.assert_not_awaited()
    mocked_local.assert_not_awaited()


@pytest.mark.asyncio
async def test_officecli_run_returns_structured_help_output() -> None:
    from agent.domains.ppt.tools import officecli_run

    with (
        patch(
            "agent.tools.officecli._execute_officecli_raw_via_gateway",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "agent.tools.officecli._run_officecli_raw_locally",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "stdout": "Usage: officecli pptx set <target> <field> <value>",
                    "stderr": "",
                    "exit_status": 0,
                }
            ),
        ),
    ):
        payload = json.loads(await officecli_run.ainvoke({"command": "pptx set shape"}))

    assert payload["kind"] == "help"
    assert "Usage:" in payload["message"]


@pytest.mark.asyncio
async def test_officecli_run_returns_structured_fatal_error() -> None:
    from agent.domains.ppt.tools import officecli_run

    with (
        patch(
            "agent.tools.officecli._execute_officecli_raw_via_gateway",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "agent.tools.officecli._run_officecli_raw_locally",
            new=AsyncMock(
                return_value={
                    "success": False,
                    "stdout": "",
                    "stderr": "Error: no such file demo.pptx",
                    "exit_status": 1,
                }
            ),
        ),
    ):
        payload = json.loads(await officecli_run.ainvoke({"command": "open demo.pptx"}))

    assert payload["success"] is False
    assert payload["kind"] == "fatal_error"
    assert "no such file" in payload["raw_stderr"].lower()


@pytest.mark.asyncio
async def test_officecli_run_returns_structured_validation_failure() -> None:
    from agent.domains.ppt.tools import officecli_run

    with (
        patch(
            "agent.tools.officecli._execute_officecli_raw_via_gateway",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "agent.tools.officecli._run_officecli_raw_locally",
            new=AsyncMock(
                return_value={
                    "success": False,
                    "stdout": "Validation failed: 2 issues found",
                    "stderr": "",
                    "exit_status": 1,
                }
            ),
        ),
    ):
        payload = json.loads(await officecli_run.ainvoke({"command": "validate demo.pptx"}))

    assert payload["success"] is False
    assert payload["kind"] == "validation_failed"


@pytest.mark.asyncio
async def test_ppt_workflow_terminates_on_inner_recursion_limit() -> None:
    from agent.domains.ppt.workflow import build_ppt_workflow_graph

    graph = build_ppt_workflow_graph()

    with (
        patch("agent.domains.ppt.workflow.build_chat_model", return_value=object()),
        patch("agent.domains.ppt.workflow.create_deep_agent", return_value=object()),
        patch(
            "agent.domains.ppt.workflow.stream_nested_graph",
            new=AsyncMock(side_effect=GraphRecursionError("loop")),
        ),
        patch("agent.domains.ppt.workflow.get_ppt_tools", return_value=[]),
        patch("agent.domains.ppt.workflow.PPT_SUBAGENTS", []),
        patch("agent.domains.ppt.workflow.build_officecli_skill_bundle", return_value=""),
    ):
        result = await graph.ainvoke(
            {
                "goal": "做一个 3 页的自我介绍 PPT",
                "task_id": "ppt-loop-test",
                "report_profile": "",
                "cost": 0.0,
                "progress": 0.0,
                "confidence": 0.0,
                "max_cost": 3.0,
                "max_steps": 15,
                "intermediate_results": [],
                "evaluations": [],
                "step_history": [],
                "coverage": {},
            }
        )

    assert result["terminal_status"] == "bounded_failure"
    assert "未收敛" in result["final_result"]
    assert result["evaluations"][-1]["passed"] is False
