import json
from dataclasses import fields

from agent.hands.scope import ToolScope
from agent.root.plan_validator import validate_and_resolve_plans
from agent.skills.policy import ResolvedPolicy


def test_policy_has_no_skill_fields():
    policy = ResolvedPolicy(
        allowed_tools=[],
        max_iterations=20,
        max_parallel_agents=5,
        require_approval_for=[],
    )
    assert not any("skill" in f.name for f in fields(policy))


def test_root_initial_state_contract_has_no_runtime_handles():
    initial_state = {
        "task_id": "task-1",
        "user_id": "user-1",
        "original_goal": "research topic",
        "conversation_context": "",
        "source_files": [],
    }
    forbidden = {
        "session",
        "tool_gateway",
        "skill_loader",
        "resolved_policy",
        "_session",
        "_tool_gateway",
        "_skill_loader",
    }
    assert forbidden.isdisjoint(initial_state)
    json.dumps(initial_state)  # Must be JSON-serializable


def test_validate_and_resolve_plans_allows_dag_dependencies():
    policy = ResolvedPolicy(
        allowed_tools=[ToolScope(name="search", capability="web.search")],
        max_iterations=20,
        max_parallel_agents=5,
        require_approval_for=[],
    )
    raw = [
        {
            "agent_id": "research",
            "agent_type": "research",
            "goal": "research topic",
            "depends_on": [],
            "allowed_tool_names": ["search"],
        },
        {
            "agent_id": "writer",
            "agent_type": "writer",
            "goal": "write summary",
            "depends_on": ["research"],
            "allowed_tool_names": ["search"],
        },
    ]
    resolved = validate_and_resolve_plans(raw, policy)
    assert [item["agent_id"] for item in resolved] == ["research", "writer"]
    assert resolved[1]["depends_on"] == ["research"]


def test_validate_and_resolve_plans_filters_disallowed_tools():
    policy = ResolvedPolicy(
        allowed_tools=[ToolScope(name="search", capability="web.search")],
        max_iterations=20,
        max_parallel_agents=5,
        require_approval_for=[],
    )
    resolved = validate_and_resolve_plans([
        {
            "agent_id": "research",
            "agent_type": "research",
            "goal": "research topic",
            "allowed_tool_names": ["search", "db_write"],
        }
    ], policy)
    assert resolved[0]["allowed_tool_names"] == ["search"]
