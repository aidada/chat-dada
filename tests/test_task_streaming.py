from __future__ import annotations

import asyncio
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from agent.platform.state import RouteDecisionPayload
from agent.runtime.interaction import ask_user
from agent.runtime.task_execution import TaskService

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://chatdada:chatdada@localhost:5432/chatdada"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1")


async def fake_orchestrator_runner(task: str, on_step, user_id: str = "anonymous") -> str:
    await on_step("🧠 fake: analyzing")
    await on_step(json.dumps({
        "type": "file",
        "url": "/download/fake.txt",
        "name": "fake.txt",
    }))
    await asyncio.sleep(0)
    return f"orchestrated for {user_id}: {task}"


async def fake_chat_runner(task: str, on_step, user_id: str = "anonymous") -> str:
    await on_step("💬 fake: answering")
    await asyncio.sleep(0)
    return f"chat for {user_id}: {task}"


async def fake_interactive_runner(task: str, on_step, user_id: str = "anonymous") -> str:
    await on_step("🔎 fake: clarifying scope")
    answer = await ask_user(
        "你更想看理论可行性，还是工程实现与实验效果？",
        context="这个选择会直接影响检索论文和输出结构。",
        placeholder="例如：更关注工程实现与实验效果",
    )
    await on_step(f"🧭 用户补充方向: {answer}")
    await asyncio.sleep(0)
    return f"interactive for {user_id}: {answer}"


async def fake_dispatcher(
    task_text: str, file_paths: list[str], mode: str = "auto", user_id: str = "anonymous"
) -> RouteDecisionPayload:
    # C7: Dispatcher deleted — return minimal placeholder payload for testing
    return {
        "route_name": "orchestrator",
        "reason": "test dispatcher stub",
        "confidence": 1.0,
        "execution_path": "research",
    }


def _emit_stream_event(payload: dict) -> None:
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(payload)
    except Exception:
        pass


async def fake_domain_runner(input_data: dict) -> SimpleNamespace:
    query = str(input_data.get("query", input_data.get("task", "")) or "")
    _emit_stream_event({"event_type": "step", "content": "🧠 fake: analyzing"})

    if "歧义" in query or "澄清方向" in query:
        answer = await ask_user(
            "你更想看理论可行性，还是工程实现与实验效果？",
            context="这个选择会直接影响检索论文和输出结构。",
            placeholder="例如：更关注工程实现与实验效果",
        )
        _emit_stream_event({"event_type": "step", "content": f"🧭 用户补充方向: {answer}"})
        return SimpleNamespace(
            result=f"interactive result: {answer}",
            artifact_refs=[],
            review={"passed": True, "issues": []},
            budget={"action": "allow", "reason": "fake interactive domain"},
            strategy="stubbed",
        )

    _emit_stream_event(
        {"event_type": "file", "url": "/download/fake.txt", "name": "fake.txt", "content": "fake.txt"}
    )
    return SimpleNamespace(
        result=f"domain result: {query}",
        artifact_refs=[{"type": "file", "name": "fake.txt", "url": "/download/fake.txt"}],
        review={"passed": True, "issues": []},
        budget={"action": "allow", "reason": "fake domain"},
        strategy="stubbed",
    )


async def slow_domain_runner(input_data: dict) -> SimpleNamespace:
    query = str(input_data.get("query", input_data.get("task", "")) or "")
    _emit_stream_event({"event_type": "step", "content": "⏳ fake: running"})
    await asyncio.sleep(30)
    return SimpleNamespace(
        result=f"slow domain result: {query}",
        artifact_refs=[],
        review={"passed": True, "issues": []},
        budget={"action": "allow", "reason": "slow fake domain"},
        strategy="stubbed",
    )


async def wait_for_terminal(service: TaskService, task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = await service.get_task(task_id)
        if snapshot and snapshot["status"] in {"succeeded", "failed", "cancelled"}:
            return snapshot
        await asyncio.sleep(0.05)
    raise AssertionError(f"Timed out waiting for task {task_id} to finish")


async def wait_for_status(
    service: TaskService,
    task_id: str,
    statuses: set[str],
    timeout: float = 5.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = await service.get_task(task_id)
        if snapshot and snapshot["status"] in statuses:
            return snapshot
        await asyncio.sleep(0.05)
    raise AssertionError(f"Timed out waiting for task {task_id} to reach {statuses}")


def assert_contains_event_types(events: list[dict], expected: list[str]) -> None:
    actual = [event["type"] for event in events]
    for event_type in expected:
        if event_type not in actual:
            raise AssertionError(f"Expected event type {event_type!r} in {actual!r}")


def wait_for_terminal_http(client: TestClient, task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/tasks/{task_id}")
        if response.status_code == 200:
            snapshot = response.json()
            if snapshot["status"] in {"succeeded", "failed", "cancelled"}:
                return snapshot
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for task {task_id} to finish")


def wait_for_status_http(
    client: TestClient,
    task_id: str,
    statuses: set[str],
    timeout: float = 5.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/tasks/{task_id}")
        if response.status_code == 200:
            snapshot = response.json()
            if snapshot["status"] in statuses:
                return snapshot
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for task {task_id} to reach {statuses}")


class TaskServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._patchers = [
            patch("agent.runtime.root_graph.domain_registry.get", side_effect=lambda _name: fake_domain_runner),
        ]
        for patcher in self._patchers:
            patcher.start()
        self.service = TaskService(TEST_DATABASE_URL, TEST_REDIS_URL)
        await self.service.connect()
        # Clean tables before each test for isolation
        async with self.service.store.pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE task_events, task_runs")

    async def asyncTearDown(self) -> None:
        await self.service.close()
        for patcher in reversed(self._patchers):
            patcher.stop()

    async def test_submit_task_routes_to_orchestrator_when_files_are_present(self) -> None:
        snapshot = await self.service.submit_task(
            task_text="hello",
            user_id="user-1",
            mode="auto",
            thinking_level="medium",
            file_paths=["/tmp/demo.txt"],
        )

        final_snapshot = await wait_for_terminal(self.service, snapshot["task_id"])
        self.assertEqual(final_snapshot["status"], "succeeded")
        self.assertEqual(final_snapshot["route_name"], "research")
        self.assertIn("attachments require tool-capable orchestration", final_snapshot["route_reason"])
        self.assertIn("domain result", final_snapshot["result"])

        events = await self.service.get_events_after(snapshot["task_id"], 0)
        assert_contains_event_types(events, ["start", "step", "task", "node", "file", "result", "monitoring"])
        self.assertEqual(events[0]["seq"], 1)
        route_event = next(event for event in events if event["type"] == "step" and "Route: research" in event.get("content", ""))
        self.assertIn("Route: research", route_event["content"])
        file_event = next(event for event in events if event["type"] == "file")
        self.assertEqual(file_event["name"], "fake.txt")
        self.assertEqual(events[-1]["type"], "monitoring")

    async def test_task_can_pause_for_user_reply_and_resume(self) -> None:
        service = TaskService(TEST_DATABASE_URL, TEST_REDIS_URL)
        await service.connect()
        try:
            snapshot = await service.submit_task(
                task_text="研究一个有歧义的问题",
                user_id="user-5",
                mode="agent",
                thinking_level="high",
                file_paths=[],
            )

            waiting_snapshot = await wait_for_status(
                service, snapshot["task_id"], {"waiting_for_user"}
            )
            self.assertEqual(waiting_snapshot["status"], "waiting_for_user")
            self.assertEqual(
                waiting_snapshot["pending_question"]["content"],
                "你更想看理论可行性，还是工程实现与实验效果？",
            )

            await service.reply_to_task(snapshot["task_id"], "更关注工程实现与实验效果")
            final_snapshot = await wait_for_terminal(service, snapshot["task_id"])
            self.assertEqual(final_snapshot["status"], "succeeded")
            self.assertIn("更关注工程实现与实验效果", final_snapshot["result"])

            events = await service.get_events_after(snapshot["task_id"], 0)
            assert_contains_event_types(events, ["start", "step", "task", "node", "question", "user_reply", "result", "monitoring"])
        finally:
            await service.close()

    async def test_waiting_task_emits_monitoring_summary_before_user_reply(self) -> None:
        service = TaskService(TEST_DATABASE_URL, TEST_REDIS_URL)
        await service.connect()
        try:
            snapshot = await service.submit_task(
                task_text="研究一个有歧义的问题",
                user_id="user-5",
                mode="agent",
                thinking_level="high",
                file_paths=[],
            )

            waiting_snapshot = await wait_for_status(
                service, snapshot["task_id"], {"waiting_for_user"}
            )
            self.assertEqual(waiting_snapshot["status"], "waiting_for_user")

            deadline = time.monotonic() + 2.0
            waiting_events: list[dict] = []
            while time.monotonic() < deadline:
                waiting_events = await service.get_events_after(snapshot["task_id"], 0)
                if any(event["type"] == "monitoring" for event in waiting_events):
                    break
                await asyncio.sleep(0.05)
            assert_contains_event_types(waiting_events, ["question", "monitoring"])
            waiting_monitoring = [event for event in waiting_events if event["type"] == "monitoring"]
            self.assertTrue(waiting_monitoring)
            self.assertTrue(waiting_monitoring[-1]["content"]["interrupted"])
            self.assertTrue(waiting_monitoring[-1]["content"]["waiting_for_user"])
            cancelled_snapshot = await service.cancel_running_task(snapshot["task_id"])
            self.assertEqual(cancelled_snapshot["status"], "cancelled")
        finally:
            await service.close()

    async def test_running_task_can_be_cancelled(self) -> None:
        with patch("agent.runtime.root_graph.domain_registry.get", return_value=slow_domain_runner):
            snapshot = await self.service.submit_task(
                task_text="帮我执行一个很慢的任务",
                user_id="user-8",
                mode="agent",
                thinking_level="medium",
                file_paths=[],
            )

            await wait_for_status(self.service, snapshot["task_id"], {"running"})
            cancelled = await self.service.cancel_running_task(snapshot["task_id"])
            self.assertEqual(cancelled["status"], "cancelled")

            final_snapshot = await wait_for_terminal(self.service, snapshot["task_id"])
            self.assertEqual(final_snapshot["status"], "cancelled")

            events = await self.service.get_events_after(snapshot["task_id"], 0)
            self.assertTrue(
                any(
                    event["type"] == "task" and event.get("status") == "cancelled"
                    for event in events
                )
            )




class TaskEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        from web.web import runtime as web_runtime

        self._web_runtime = web_runtime
        self.original_service = web_runtime.task_service
        self._patchers = [
            patch("agent.runtime.root_graph.domain_registry.get", side_effect=lambda _name: fake_domain_runner),
        ]
        for patcher in self._patchers:
            patcher.start()
        # TestClient lifespan will call connect()/close() automatically
        web_runtime.task_service = TaskService(TEST_DATABASE_URL, TEST_REDIS_URL)

    def tearDown(self) -> None:
        self._web_runtime.task_service = self.original_service
        for patcher in reversed(self._patchers):
            patcher.stop()

    def test_post_get_and_replay_events(self) -> None:
        with TestClient(main.app) as client:
            create_response = client.post(
                "/tasks",
                json={
                    "task": "hello",
                    "user_id": "user-2",
                    "mode": "auto",
                    "thinking_level": "high",
                    "file_paths": [],
                },
            )
            self.assertEqual(create_response.status_code, 202)

            task_id = create_response.json()["task_id"]
            snapshot = wait_for_terminal_http(client, task_id)
            self.assertEqual(snapshot["status"], "succeeded")
            self.assertEqual(snapshot["route_name"], "general_chat")
            self.assertGreaterEqual(snapshot["last_seq"], 4)

            with client.stream("GET", f"/tasks/{task_id}/events?after_seq=1") as stream_response:
                body = "".join(stream_response.iter_text())

            self.assertEqual(stream_response.status_code, 200)
            self.assertNotIn("event: start", body)
            self.assertIn("event: step", body)
            self.assertIn("event: result", body)
            self.assertIn("event: monitoring", body)
            self.assertIn("Route: general_chat", body)

    def test_chat_mode_rejects_attachments(self) -> None:
        with TestClient(main.app) as client:
            response = client.post(
                "/tasks",
                json={
                    "task": "hi",
                    "user_id": "user-4",
                    "mode": "chat",
                    "thinking_level": "medium",
                    "file_paths": ["/tmp/a.txt"],
                },
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("chat 模式暂不支持附件", response.json()["detail"])

    def test_reply_endpoint_resumes_waiting_task(self) -> None:
        main.task_service = TaskService(
            TEST_DATABASE_URL,
            TEST_REDIS_URL,
        )

        with TestClient(main.app) as client:
            create_response = client.post(
                "/tasks",
                json={
                    "task": "帮我做一个需要澄清方向的深度研究",
                    "user_id": "user-6",
                    "mode": "agent",
                    "thinking_level": "high",
                    "file_paths": [],
                },
            )
            self.assertEqual(create_response.status_code, 202)

            task_id = create_response.json()["task_id"]
            waiting_snapshot = wait_for_status_http(client, task_id, {"waiting_for_user"})
            self.assertEqual(waiting_snapshot["status"], "waiting_for_user")
            self.assertIn("理论可行性", waiting_snapshot["pending_question"]["content"])

            reply_response = client.post(
                f"/tasks/{task_id}/reply",
                json={"answer": "更关注理论可行性"},
            )
            self.assertEqual(reply_response.status_code, 202)

            snapshot = wait_for_terminal_http(client, task_id)
            self.assertEqual(snapshot["status"], "succeeded")
            self.assertIn("更关注理论可行性", snapshot["result"])

            with client.stream("GET", f"/tasks/{task_id}/events") as stream_response:
                body = "".join(stream_response.iter_text())

            self.assertEqual(stream_response.status_code, 200)
            self.assertIn("event: question", body)
            self.assertIn("event: user_reply", body)

    def test_task_metadata_endpoints_expose_replay_artifacts_review_and_traces(self) -> None:
        with TestClient(main.app) as client:
            create_response = client.post(
                "/tasks",
                json={
                    "task": "hi",
                    "user_id": "user-7",
                    "mode": "auto",
                    "thinking_level": "medium",
                    "file_paths": [],
                },
            )
            self.assertEqual(create_response.status_code, 202)
            task_id = create_response.json()["task_id"]
            wait_for_terminal_http(client, task_id)

            artifacts = client.get(f"/tasks/{task_id}/artifacts")
            self.assertEqual(artifacts.status_code, 200)
            self.assertEqual(artifacts.json()["artifact_refs"], [])

            review = client.get(f"/tasks/{task_id}/review")
            self.assertEqual(review.status_code, 200)
            self.assertIn("review", review.json())
            self.assertIn("budget", review.json())

            replay = client.get(f"/tasks/{task_id}/replay")
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json()["task"]["task_id"], task_id)
            self.assertTrue(replay.json()["events"])

            trace = client.get(f"/tasks/{task_id}/trace")
            self.assertEqual(trace.status_code, 200)
            self.assertIn("trace", trace.json())

            traces = client.get("/api/traces")
            self.assertEqual(traces.status_code, 200)
            self.assertIn("items", traces.json())
