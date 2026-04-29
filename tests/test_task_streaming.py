from __future__ import annotations

import asyncio
import os
import time
import unittest
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from langgraph.types import Command

import main
from agent.runtime.task_execution import TaskService
from agent.session.runtime import SessionRuntime
from domain.tasks.session_store import TaskEventRecord, TaskProjectionRecord
from web import runtime as web_runtime

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://chatdada:chatdada@localhost:5432/chatdada"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1")


class FakeRootGraph:
    def __init__(self) -> None:
        self._final_values: dict[str, object] = {}

    async def astream(self, stream_input, config=None, version=None, stream_mode=None, subgraphs=None):
        resume_value = getattr(stream_input, "resume", None) if isinstance(stream_input, Command) else None
        initial_state = stream_input if isinstance(stream_input, dict) else {}
        task_text = str(initial_state.get("task_text", "") or "")
        execution_task = str(initial_state.get("execution_task", task_text) or task_text)

        yield self._task_started("run_coordinator")

        if resume_value is not None:
            answer = str(resume_value or "")
            yield self._custom({"event_type": "step", "content": f"🧭 用户补充方向: {answer}"})
            self._final_values = {
                "final_result": f"interactive result: {answer}",
                "artifact_refs": [],
                "review": {"passed": True, "issues": []},
                "budget": {"action": "allow", "reason": "fake interactive domain"},
            }
            yield self._node_update("run_research", self._final_values)
            yield self._task_finished("run_coordinator")
            return

        if "很慢" in task_text:
            yield self._custom({"event_type": "step", "content": "⏳ fake: running"})
            await asyncio.sleep(30)
            return

        if "歧义" in task_text or "澄清方向" in task_text:
            yield self._custom({"event_type": "step", "content": "🔎 fake: clarifying scope"})
            yield {
                "type": "updates",
                "data": {
                    "__interrupt__": [
                        SimpleNamespace(
                            value={
                                "content": "你更想看理论可行性，还是工程实现与实验效果？",
                                "context": "这个选择会直接影响检索论文和输出结构。",
                                "placeholder": "例如：更关注工程实现与实验效果",
                            }
                        )
                    ]
                },
            }
            self._final_values = {}
            return

        yield self._custom({"event_type": "step", "content": "🧠 fake: analyzing"})
        artifact_refs: list[dict[str, str]] = []
        if "[用户上传了以下文件" in execution_task:
            artifact_refs = [{"type": "file", "name": "fake.txt", "url": "/download/fake.txt"}]
            yield self._custom(
                {
                    "event_type": "file",
                    "name": "fake.txt",
                    "url": "/download/fake.txt",
                    "content": "fake.txt",
                }
            )

        self._final_values = {
            "final_result": f"domain result: {task_text}",
            "artifact_refs": artifact_refs,
            "review": {"passed": True, "issues": []},
            "budget": {"action": "allow", "reason": "fake domain"},
        }
        yield self._node_update("run_research", self._final_values)
        yield self._task_finished("run_coordinator")

    async def aget_state(self, config):
        return SimpleNamespace(values=dict(self._final_values))

    def _custom(self, payload: dict[str, object]) -> dict[str, object]:
        return {"type": "custom", "data": payload}

    def _node_update(self, node_name: str, payload: dict[str, object]) -> dict[str, object]:
        return {"type": "updates", "data": {node_name: payload}}

    def _task_started(self, task_name: str) -> dict[str, object]:
        return {
            "type": "tasks",
            "data": {"id": "lg-task-1", "name": task_name, "input": {}, "triggers": ["start"]},
        }

    def _task_finished(self, task_name: str) -> dict[str, object]:
        return {
            "type": "tasks",
            "data": {"id": "lg-task-1", "name": task_name, "result": {"ok": True}, "interrupts": []},
        }


class MemorySessionStore:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskProjectionRecord] = {}
        self._events: dict[str, list[TaskEventRecord]] = {}

    async def setup(self) -> None:
        return None

    async def append_event(self, *, task_id: str, event_type: str, payload: dict[str, object]) -> TaskEventRecord:
        seq = len(self._events.setdefault(task_id, [])) + 1
        record = TaskEventRecord(
            task_id=task_id,
            seq=seq,
            event_type=event_type,
            payload=dict(payload),
            created_at=datetime.now(UTC),
        )
        self._events[task_id].append(record)
        projection = self._tasks.get(task_id)
        if projection is not None:
            projection.last_seq = seq
            projection.updated_at = record.created_at
        return record

    async def list_events_after(self, *, task_id: str, after_seq: int) -> list[TaskEventRecord]:
        return [event for event in self._events.get(task_id, []) if event.seq > after_seq]

    async def create_task(
        self,
        *,
        user_id: str,
        task_text: str,
        mode: str,
        thinking_level: str,
        request_payload: dict[str, object],
        conversation_id: str = "",
    ) -> TaskProjectionRecord:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        projection = TaskProjectionRecord(
            task_id=task_id,
            user_id=user_id,
            status="queued",
            task_text=task_text,
            mode=mode,
            thinking_level=thinking_level,
            request_payload=dict(request_payload),
            conversation_id=conversation_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self._tasks[task_id] = projection
        self._events[task_id] = []
        return projection

    async def get_projection(self, task_id: str) -> TaskProjectionRecord | None:
        projection = self._tasks.get(task_id)
        if projection is None:
            return None
        projection.last_seq = len(self._events.get(task_id, []))
        return projection

    async def list_interrupted_task_ids(self) -> list[str]:
        return [
            task_id
            for task_id, projection in self._tasks.items()
            if projection.status in {"queued", "running", "waiting_for_user"}
        ]

    async def update_projection(
        self,
        task_id: str,
        *,
        projection_patch: dict[str, object] | None = None,
        request_payload_patch: dict[str, object] | None = None,
        clear_request_payload_keys: Sequence[str] = (),
    ) -> TaskProjectionRecord | None:
        projection = self._tasks.get(task_id)
        if projection is None:
            return None
        if projection_patch:
            for key, value in projection_patch.items():
                setattr(projection, key, value)
        if request_payload_patch or clear_request_payload_keys:
            payload = dict(projection.request_payload or {})
            for key in clear_request_payload_keys:
                payload.pop(str(key), None)
            if request_payload_patch:
                payload.update(request_payload_patch)
            projection.request_payload = payload
        projection.updated_at = datetime.now(UTC)
        projection.last_seq = len(self._events.get(task_id, []))
        return projection


class FakePubSub:
    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self._channels: set[str] = set()

    async def subscribe(self, channel: str) -> None:
        self._channels.add(channel)
        self._redis._subscribers.setdefault(channel, set()).add(self)

    async def unsubscribe(self, channel: str) -> None:
        self._channels.discard(channel)
        self._redis._subscribers.get(channel, set()).discard(self)

    async def aclose(self) -> None:
        for channel in list(self._channels):
            await self.unsubscribe(channel)

    async def get_message(self, ignore_subscribe_messages=True, timeout=None):
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None


class FakeRedis:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[FakePubSub]] = {}
        self._values: dict[str, str] = {}
        self.connection_pool = None

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self)

    async def publish(self, channel: str, data: str) -> None:
        for subscriber in list(self._subscribers.get(channel, set())):
            await subscriber._queue.put({"type": "message", "data": data})

    async def set(self, key: str, value: str, ex=None) -> None:
        self._values[key] = value

    async def get(self, key: str):
        return self._values.get(key)

    async def aclose(self) -> None:
        self._subscribers.clear()
        self._values.clear()


class FakeQuotaService:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def assess_before_task(self, user_id: str):
        return []

    def estimate_cost_from_usage(self, llm_usage):
        return 0.0

    def estimate_cost_usd(self, *args, **kwargs):
        return 0.0

    async def record_task_usage(self, **kwargs) -> None:
        return None


def _fake_session_factory():
    session = SimpleNamespace(commit=AsyncMock())

    class _CM:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    return _CM()


def build_test_service() -> TaskService:
    redis = FakeRedis()
    session = SessionRuntime(redis, MemorySessionStore())
    return TaskService(
        session=session,
        redis=redis,
        checkpointer_factory=lambda: object(),
        conversation_context_builder_factory=lambda: SimpleNamespace(
            build=AsyncMock(return_value=SimpleNamespace(text="", strategy="none", round_count=0))
        ),
        embedding_service=SimpleNamespace(generate_embeddings=AsyncMock()),
        conversation_service=SimpleNamespace(update_summary=AsyncMock()),
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
            patch("agent.runtime.task_execution.build_root_graph", return_value=FakeRootGraph()),
            patch("agent.runtime.task_execution.QuotaService", FakeQuotaService),
            patch("agent.runtime.task_execution.SessionFactory", _fake_session_factory),
        ]
        for patcher in self._patchers:
            patcher.start()
        self.service = build_test_service()
        await self.service.connect()

    async def asyncTearDown(self) -> None:
        await self.service.close()
        for patcher in reversed(self._patchers):
            patcher.stop()

    async def test_submit_task_records_runtime_events_and_result(self) -> None:
        snapshot = await self.service.submit_task(
            task_text="hello",
            user_id="user-1",
            mode="auto",
            thinking_level="medium",
            file_paths=["/tmp/demo.txt"],
        )

        final_snapshot = await wait_for_terminal(self.service, snapshot["task_id"])
        self.assertEqual(final_snapshot["status"], "succeeded")
        self.assertEqual(final_snapshot["route_name"], "general_chat")
        self.assertIn("domain result", final_snapshot["result"])

        events = await self.service.get_events_after(snapshot["task_id"], 0)
        assert_contains_event_types(events, ["lifecycle.started", "progress.step", "artifact.created", "lifecycle.completed"])
        self.assertEqual(events[0]["seq"], 1)
        file_event = next(event for event in events if event["type"] == "artifact.created")
        self.assertEqual(file_event["payload"]["name"], "fake.txt")
        self.assertEqual(events[-1]["type"], "lifecycle.completed")

    async def test_task_can_pause_for_user_reply_and_resume(self) -> None:
        snapshot = await self.service.submit_task(
            task_text="研究一个有歧义的问题",
            user_id="user-5",
            mode="agent",
            thinking_level="high",
            file_paths=[],
        )

        waiting_snapshot = await wait_for_status(self.service, snapshot["task_id"], {"waiting_for_user"})
        self.assertEqual(waiting_snapshot["status"], "waiting_for_user")
        self.assertEqual(
            waiting_snapshot["pending_question"]["content"],
            "你更想看理论可行性，还是工程实现与实验效果？",
        )

        await self.service.reply_to_task(snapshot["task_id"], "更关注工程实现与实验效果")
        final_snapshot = await wait_for_terminal(self.service, snapshot["task_id"])
        self.assertEqual(final_snapshot["status"], "succeeded")
        self.assertIn("更关注工程实现与实验效果", final_snapshot["result"])

        events = await self.service.get_events_after(snapshot["task_id"], 0)
        assert_contains_event_types(events, ["lifecycle.started", "progress.step", "interaction.question", "interaction.answer", "lifecycle.completed"])

    async def test_waiting_task_emits_monitoring_summary_before_user_reply(self) -> None:
        snapshot = await self.service.submit_task(
            task_text="研究一个有歧义的问题",
            user_id="user-5",
            mode="agent",
            thinking_level="high",
            file_paths=[],
        )

        waiting_snapshot = await wait_for_status(self.service, snapshot["task_id"], {"waiting_for_user"})
        self.assertEqual(waiting_snapshot["status"], "waiting_for_user")

        deadline = time.monotonic() + 2.0
        waiting_events: list[dict] = []
        while time.monotonic() < deadline:
            waiting_events = await self.service.get_events_after(snapshot["task_id"], 0)
            if any(event["type"] == "interaction.question" for event in waiting_events):
                break
            await asyncio.sleep(0.05)
        assert_contains_event_types(waiting_events, ["interaction.question"])

    async def test_running_task_can_be_cancelled(self) -> None:
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
                event["type"] == "lifecycle.cancelled"
                for event in events
            )
        )


def test_task_service_uses_coordinator_root_graph() -> None:
    from agent.runtime import task_execution

    assert task_execution.build_root_graph.__module__ == "agent.runtime.root_graph"


class TaskEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        async def _fake_resolve_current_user_once_with_metadata(_request):
            return SimpleNamespace(id="anonymous"), {"auth_lookup_ms": 0.0}

        self._patchers = [
            patch("agent.runtime.task_execution.build_root_graph", return_value=FakeRootGraph()),
            patch("agent.runtime.task_execution.QuotaService", FakeQuotaService),
            patch("agent.runtime.task_execution.SessionFactory", _fake_session_factory),
            patch("web.routers.tasks.resolve_current_user_once_with_metadata", _fake_resolve_current_user_once_with_metadata),
        ]
        for patcher in self._patchers:
            patcher.start()

        self.original_runtime_service = web_runtime.task_service
        self.original_main_service = main.task_service
        replacement = build_test_service()
        web_runtime.task_service = replacement
        main.task_service = replacement
        from web.deps import get_current_user

        main.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="anonymous")

    def tearDown(self) -> None:
        web_runtime.task_service = self.original_runtime_service
        main.task_service = self.original_main_service
        main.app.dependency_overrides.clear()
        for patcher in reversed(self._patchers):
            patcher.stop()

    def test_post_get_and_replay_events(self) -> None:
        with TestClient(main.app) as client:
            create_response = client.post(
                "/tasks",
                json={
                    "task": "hello",
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
            self.assertIn("event: progress.step", body)
            self.assertIn("event: lifecycle.completed", body)
            self.assertNotIn("Route: general_chat", body)

    def test_chat_mode_rejects_attachments(self) -> None:
        with TestClient(main.app) as client:
            response = client.post(
                "/tasks",
                json={
                    "task": "hi",
                    "mode": "chat",
                    "thinking_level": "medium",
                    "file_paths": ["/tmp/a.txt"],
                },
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("chat 模式暂不支持附件", response.json()["detail"])

    def test_reply_endpoint_resumes_waiting_task(self) -> None:
        with TestClient(main.app) as client:
            create_response = client.post(
                "/tasks",
                json={
                    "task": "帮我做一个需要澄清方向的深度研究",
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
            self.assertIn("event: interaction.question", body)
            self.assertIn("event: interaction.answer", body)
            self.assertIn("event: lifecycle.completed", body)

    def test_task_metadata_endpoints_expose_replay_artifacts_review_and_traces(self) -> None:
        with TestClient(main.app) as client:
            create_response = client.post(
                "/tasks",
                json={
                    "task": "hi",
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
