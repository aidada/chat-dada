import pytest

from agent.hands.protocol import ToolCall, ToolContext, ToolResult
from agent.hands.scope import ToolScope
from agent.skills.policy import ResolvedPolicy
from agent.sub_graphs.base import ask_user
from agent.sub_graphs.state import AgentState, SkillContext


class _FakeSession:
    """Minimal mock session with an async emit_event no-op."""

    async def emit_event(self, task_id, event_type, data):
        pass


@pytest.mark.asyncio
async def test_subgraph_ask_user_returns_intent_without_interrupt(monkeypatch):
    called = False

    def fake_interrupt(_payload):
        nonlocal called
        called = True
        raise AssertionError("Sub Graph must not call request_interrupt directly")

    monkeypatch.setattr("agent.platform.interrupts.request_interrupt", fake_interrupt)
    skill_context = SkillContext(
        agent_id="research",
        root_task_id="task-1",
        root_user_id="user-1",
        checkpoint_ns="research",
        trace_id="task-1:research",
    )
    monkeypatch.setattr(
        "langgraph.config.get_config",
        lambda: {"configurable": {"skill_context": skill_context}},
    )

    state = AgentState(
        agent_id="research",
        messages=[{
            "role": "assistant",
            "content": '{"action":"ask_user","user_question":"需要补充什么范围？"}',
        }],
        iteration=1,
    )

    result = await ask_user(state)

    assert called is False
    assert result["status"] == "waiting_for_user"
    assert result["resume_metadata"]["agent_id"] == "research"
    assert result["resume_metadata"]["question"]


@pytest.mark.asyncio
async def test_tool_gateway_uses_context_policy_not_global_state():
    from agent.hands.gateway import ToolGateway
    from agent.hands.local_executor import LocalToolExecutor

    gateway = ToolGateway(local=LocalToolExecutor(), session=_FakeSession())

    class FakeExecutor:
        async def execute(self, call, ctx):
            return ToolResult(success=True, output="ok")

        async def prepare(self, *_args):
            return None

    gateway._local = FakeExecutor()

    search_policy = ResolvedPolicy(
        allowed_tools=[ToolScope(name="search", capability="web.search")],
        max_iterations=20,
        max_parallel_agents=5,
        require_approval_for=[],
    )
    write_policy = ResolvedPolicy(
        allowed_tools=[ToolScope(name="write", capability="file.write")],
        max_iterations=20,
        max_parallel_agents=5,
        require_approval_for=[],
    )

    assert (await gateway.execute(
        ToolCall(tool_name="search", params={}, task_id="t1"),
        ToolContext(user_id="u1", task_id="t1", policy=search_policy),
    )).success is True
    assert (await gateway.execute(
        ToolCall(tool_name="search", params={}, task_id="t2"),
        ToolContext(user_id="u2", task_id="t2", policy=write_policy),
    )).success is False
