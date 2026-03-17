from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main
from task_dispatcher import RouteDecision, route_task_request
from task_interaction import ask_user
from task_runtime import TaskService


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


async def fake_dispatcher(task_text: str, file_paths: list[str], mode: str = "auto", user_id: str = "anonymous") -> RouteDecision:
    route_name, reason, confidence = route_task_request(task_text, file_paths, mode)
    executor = fake_chat_runner if route_name == "general_chat" else fake_orchestrator_runner
    return RouteDecision(
        route_name=route_name,
        reason=reason,
        executor=executor,
        confidence=confidence,
    )


async def wait_for_terminal(service: TaskService, task_id: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = service.get_task(task_id)
        if snapshot and snapshot["status"] in {"succeeded", "failed", "cancelled"}:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for task {task_id} to finish")


async def wait_for_status(
    service: TaskService,
    task_id: str,
    statuses: set[str],
    timeout: float = 2.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = service.get_task(task_id)
        if snapshot and snapshot["status"] in statuses:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for task {task_id} to reach {statuses}")


def wait_for_terminal_http(client: TestClient, task_id: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/tasks/{task_id}")
        if response.status_code == 200:
            snapshot = response.json()
            if snapshot["status"] in {"succeeded", "failed", "cancelled"}:
                return snapshot
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for task {task_id} to finish")


def wait_for_status_http(
    client: TestClient,
    task_id: str,
    statuses: set[str],
    timeout: float = 2.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/tasks/{task_id}")
        if response.status_code == 200:
            snapshot = response.json()
            if snapshot["status"] in statuses:
                return snapshot
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for task {task_id} to reach {statuses}")


class TaskServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.service = TaskService(Path(self.tmpdir.name) / "tasks.sqlite3", dispatcher=fake_dispatcher)

    async def asyncTearDown(self) -> None:
        self.tmpdir.cleanup()

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
        self.assertEqual(final_snapshot["route_name"], "orchestrator")
        self.assertIn("attachments require tool-capable orchestration", final_snapshot["route_reason"])
        self.assertIn("orchestrated for user-1", final_snapshot["result"])

        events = self.service.get_events_after(snapshot["task_id"], 0)
        self.assertEqual(
            [event["type"] for event in events],
            ["start", "step", "step", "file", "result", "monitoring"],
        )
        self.assertEqual(events[0]["seq"], 1)
        self.assertIn("Route: orchestrator", events[1]["content"])
        self.assertEqual(events[3]["name"], "fake.txt")
        self.assertEqual(events[-1]["type"], "monitoring")

    async def test_submit_task_routes_to_general_chat_for_simple_greeting(self) -> None:
        snapshot = await self.service.submit_task(
            task_text="hi",
            user_id="user-3",
            mode="auto",
            thinking_level="medium",
            file_paths=[],
        )

        final_snapshot = await wait_for_terminal(self.service, snapshot["task_id"])
        self.assertEqual(final_snapshot["status"], "succeeded")
        self.assertEqual(final_snapshot["route_name"], "general_chat")
        self.assertIn("direct chat", final_snapshot["route_reason"])
        self.assertIn("chat for user-3", final_snapshot["result"])

        events = self.service.get_events_after(snapshot["task_id"], 0)
        self.assertIn("Route: general_chat", events[1]["content"])

    async def test_task_can_pause_for_user_reply_and_resume(self) -> None:
        async def interactive_dispatcher(
            task_text: str,
            file_paths: list[str],
            mode: str = "auto",
            user_id: str = "anonymous",
        ) -> RouteDecision:
            return RouteDecision(
                route_name="orchestrator",
                reason="interactive research test",
                executor=fake_interactive_runner,
                confidence=1.0,
            )

        service = TaskService(Path(self.tmpdir.name) / "interactive.sqlite3", dispatcher=interactive_dispatcher)
        snapshot = await service.submit_task(
            task_text="研究一个有歧义的问题",
            user_id="user-5",
            mode="agent",
            thinking_level="high",
            file_paths=[],
        )

        waiting_snapshot = await wait_for_status(service, snapshot["task_id"], {"waiting_for_user"})
        self.assertEqual(waiting_snapshot["status"], "waiting_for_user")
        self.assertEqual(
            waiting_snapshot["pending_question"]["content"],
            "你更想看理论可行性，还是工程实现与实验效果？",
        )

        await service.reply_to_task(snapshot["task_id"], "更关注工程实现与实验效果")
        final_snapshot = await wait_for_terminal(service, snapshot["task_id"])
        self.assertEqual(final_snapshot["status"], "succeeded")
        self.assertIn("更关注工程实现与实验效果", final_snapshot["result"])

        events = service.get_events_after(snapshot["task_id"], 0)
        self.assertEqual(
            [event["type"] for event in events],
            ["start", "step", "step", "question", "user_reply", "step", "result", "monitoring"],
        )


class TaskRoutingTests(unittest.TestCase):
    def test_mode_chat_forces_general_chat(self) -> None:
        route_name, reason, confidence = route_task_request("帮我搜索 AI 新闻", [], "chat")
        self.assertEqual(route_name, "general_chat")
        self.assertEqual(reason, "forced by mode=chat")
        self.assertEqual(confidence, 1.0)

    def test_mode_agent_forces_orchestrator(self) -> None:
        route_name, reason, confidence = route_task_request("hi", [], "agent")
        self.assertEqual(route_name, "orchestrator")
        self.assertEqual(reason, "forced by mode=agent")
        self.assertEqual(confidence, 1.0)

    def test_auto_routes_simple_question_to_general_chat(self) -> None:
        route_name, reason, confidence = route_task_request("解释一下 FastAPI 是什么", [], "auto")
        self.assertEqual(route_name, "general_chat")
        self.assertIn("direct chat", reason)
        self.assertGreater(confidence, 0.5)

    def test_auto_routes_task_request_to_orchestrator(self) -> None:
        route_name, reason, confidence = route_task_request("帮我搜索今天的 AI 新闻并整理成报告", [], "auto")
        self.assertEqual(route_name, "orchestrator")
        self.assertIn("keywords", reason)
        self.assertGreater(confidence, 0.5)

    def test_auto_routes_research_request_to_orchestrator_even_if_it_mentions_how(self) -> None:
        route_name, reason, confidence = route_task_request(
            "对于如何构建多路径时空图仍然没研究，我想要识别 NLOS 信号，同时还要构建纯 GNSS 提取的城市多路径时空图，用于后续 GNSS 定位抑制多路径误差",
            [],
            "auto",
        )
        self.assertEqual(route_name, "orchestrator")
        self.assertIn("research", reason)
        self.assertGreater(confidence, 0.5)


class TaskEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_service = main.task_service
        main.task_service = TaskService(Path(self.tmpdir.name) / "tasks.sqlite3", dispatcher=fake_dispatcher)

    def tearDown(self) -> None:
        main.task_service = self.original_service
        self.tmpdir.cleanup()

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
        async def interactive_dispatcher(
            task_text: str,
            file_paths: list[str],
            mode: str = "auto",
            user_id: str = "anonymous",
        ) -> RouteDecision:
            return RouteDecision(
                route_name="orchestrator",
                reason="interactive research test",
                executor=fake_interactive_runner,
                confidence=1.0,
            )

        main.task_service = TaskService(
            Path(self.tmpdir.name) / "interactive-http.sqlite3",
            dispatcher=interactive_dispatcher,
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
