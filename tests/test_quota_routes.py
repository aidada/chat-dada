from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.web.deps import get_admin_user, get_current_user, get_quota_service
from apps.web.routers.quotas import router as quota_router
from domain.billing.schemas import UserQuotaView


class _FakeUser:
    def __init__(self, *, user_id: str = "user_1", email: str = "admin@example.com") -> None:
        self.id = user_id
        self.email = email


class _FakeQuotaService:
    async def get_user_quota_view(self, *, user_id: str, scope: str = "default") -> UserQuotaView:
        return UserQuotaView(
            user_id=user_id,
            scope=scope,
            enabled=True,
            active_block_reason="",
            periods=[
                {
                    "period": "daily",
                    "tasks_used": 2,
                    "tasks_limit": 20,
                    "tasks_remaining": 18,
                    "tokens_used": 1200,
                    "tokens_limit": 50000,
                    "tokens_remaining": 48800,
                    "cost_used_usd": 0.12,
                    "cost_limit_usd": 5.0,
                    "cost_remaining_usd": 4.88,
                    "reset_at": "2026-03-25T00:00:00+00:00",
                    "blocked_reason": "",
                }
            ],
        )

    async def upsert_user_quota(self, *, user_id: str, payload):
        return await self.get_user_quota_view(user_id=user_id, scope=payload.scope)


class QuotaRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(quota_router)
        app.dependency_overrides[get_current_user] = lambda: _FakeUser()
        app.dependency_overrides[get_admin_user] = lambda: _FakeUser()
        app.dependency_overrides[get_quota_service] = lambda: _FakeQuotaService()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_get_my_quota(self) -> None:
        response = self.client.get("/me/quota")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["user_id"], "user_1")
        self.assertEqual(payload["periods"][0]["tasks_limit"], 20)

    def test_admin_can_update_quota(self) -> None:
        response = self.client.put(
            "/admin/users/user_2/quota",
            json={
                "scope": "default",
                "enabled": True,
                "daily_task_limit": 20,
                "weekly_task_limit": 100,
                "monthly_task_limit": 300,
                "daily_token_limit": 100000,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["user_id"], "user_2")
        self.assertTrue(payload["enabled"])


if __name__ == "__main__":
    unittest.main()
