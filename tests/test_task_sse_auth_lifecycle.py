from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from web import runtime as web_runtime


class _FakeRequest:
    def __init__(self) -> None:
        self._disconnected = False

    async def is_disconnected(self) -> bool:
        return self._disconnected


class _FakePubSub:
    def __init__(self) -> None:
        self._sent = False

    async def get_message(self, ignore_subscribe_messages=True, timeout=None):
        if self._sent:
            return None
        self._sent = True
        return {
            "type": "message",
            "data": json.dumps({"seq": 1, "type": "step", "content": "hello"}),
        }


class _FakeTaskService:
    def __init__(self) -> None:
        self.pubsub = _FakePubSub()
        self.unsubscribed = False
        self.redis = SimpleNamespace(connection_pool=None)

    async def subscribe(self, task_id: str):
        return self.pubsub

    async def unsubscribe(self, task_id: str, pubsub) -> None:
        self.unsubscribed = True

    async def get_events_after(self, task_id: str, after_seq: int):
        return []

    async def get_task(self, task_id: str):
        return {"task_id": task_id, "status": "succeeded"}


class TaskSSEAuthLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_stream_response_includes_stream_meta(self) -> None:
        fake_service = _FakeTaskService()
        original_service = web_runtime.task_service
        web_runtime.task_service = fake_service
        try:
            response = await web_runtime.event_stream_response(
                _FakeRequest(),
                "task_test",
                0,
                snapshot={"task_id": "task_test", "status": "running"},
                stream_metadata={"auth_lookup_ms": 3.21},
            )
            body = ""
            async for chunk in response.body_iterator:
                body += chunk.decode() if isinstance(chunk, bytes) else chunk
        finally:
            web_runtime.task_service = original_service

        self.assertIn('"stream_meta"', body)
        self.assertIn('"auth_lookup_ms": 3.21', body)
        self.assertTrue(fake_service.unsubscribed)
