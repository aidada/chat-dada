import pytest
from agent.hands.scope import ToolScope
from agent.hands.gateway import ToolGateway
from agent.hands.local_executor import LocalToolExecutor
from agent.hands.protocol import ToolCall, ToolContext, ToolResult


class _FakeSession:
    """Minimal mock session with an async emit_event no-op."""
    async def emit_event(self, task_id, event_type, data):
        pass


@pytest.mark.asyncio
async def test_gateway_blocks_disallowed_tool():
    gw = ToolGateway(local=LocalToolExecutor(), session=_FakeSession())
    policy = type("Policy", (), {"allowed_tools": [
        ToolScope(name="allowed_search", capability="web.search"),
    ]})()
    call = ToolCall(
        tool_name="blocked_tool",
        params={"query": "test"},
        task_id="t1",
    )
    ctx = ToolContext(user_id="u1", task_id="t1", policy=policy)
    result = await gw.execute(call, ctx)
    assert result.success is False
    assert "not allowed" in str(result.error or "").lower()


@pytest.mark.asyncio
async def test_gateway_allows_registered_tool():
    gw = ToolGateway(local=LocalToolExecutor(), session=_FakeSession())
    policy = type("Policy", (), {"allowed_tools": [
        ToolScope(name="allowed_search", capability="web.search"),
    ]})()

    async def fake_execute(self, call, ctx):
        return ToolResult(success=True, output="{}")

    async def fake_prepare(self, call, ctx):
        pass

    gw._local = type("Fake", (), {"execute": fake_execute, "prepare": fake_prepare})()

    call = ToolCall(
        tool_name="allowed_search",
        params={"query": "test"},
        task_id="t1",
    )
    ctx = ToolContext(user_id="u1", task_id="t1", policy=policy)
    result = await gw.execute(call, ctx)
    assert result.success is True


@pytest.mark.asyncio
async def test_gateway_policy_is_per_context_not_global():
    gw = ToolGateway(local=LocalToolExecutor(), session=_FakeSession())

    async def fake_execute(self, call, ctx):
        return ToolResult(success=True, output="{}")

    async def fake_prepare(self, call, ctx):
        pass

    gw._local = type("Fake", (), {"execute": fake_execute, "prepare": fake_prepare})()

    search_policy = type("Policy", (), {"allowed_tools": [
        ToolScope(name="search", capability="web.search"),
    ]})()
    write_policy = type("Policy", (), {"allowed_tools": [
        ToolScope(name="write", capability="file.write"),
    ]})()

    search_call = ToolCall(tool_name="search", params={}, task_id="t1")
    write_call = ToolCall(tool_name="write", params={}, task_id="t2")

    assert (await gw.execute(search_call, ToolContext(user_id="u1", task_id="t1", policy=search_policy))).success is True
    assert (await gw.execute(search_call, ToolContext(user_id="u2", task_id="t2", policy=write_policy))).success is False
    assert (await gw.execute(write_call, ToolContext(user_id="u2", task_id="t2", policy=write_policy))).success is True
