from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.web.deps import get_auth_service, get_current_user
from apps.web.routers.auth import router as auth_router


class _FakeUser:
    def __init__(
        self,
        *,
        user_id: str = "user_1",
        email: str = "u@example.com",
        email_verified: bool = True,
        display_name: str = "User One",
        avatar_url: str = "",
    ) -> None:
        self.id = user_id
        self.email = email
        self.email_verified = email_verified
        self.display_name = display_name
        self.avatar_url = avatar_url


class _FakeAuthService:
    async def register_with_password(self, *, email: str, password: str, display_name: str = ""):
        return _FakeUser(email=email, display_name=display_name or "registered")

    async def login_with_password(self, *, email: str, password: str):
        return _FakeUser(email=email, display_name="logged-in")

    async def create_user_session(self, *, user_id: str, user_agent: str, ip_address: str):
        return "session-token", datetime.now(UTC) + timedelta(days=7)

    async def logout_by_session_token(self, token: str) -> None:
        return None


class AuthRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(auth_router)
        app.dependency_overrides[get_auth_service] = lambda: _FakeAuthService()
        app.dependency_overrides[get_current_user] = lambda: _FakeUser()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_register_sets_session_cookie(self) -> None:
        response = self.client.post(
            "/auth/register",
            json={"email": "new@example.com", "password": "secret-123", "display_name": "New User"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["email"], "new@example.com")
        self.assertIn("chat_dada_session=session-token", response.headers.get("set-cookie", ""))

    def test_login_sets_session_cookie(self) -> None:
        response = self.client.post(
            "/auth/login",
            json={"email": "login@example.com", "password": "secret-123"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["display_name"], "logged-in")
        self.assertIn("chat_dada_session=session-token", response.headers.get("set-cookie", ""))

    def test_me_returns_current_user(self) -> None:
        response = self.client.get("/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["id"], "user_1")

    def test_logout_clears_cookie(self) -> None:
        response = self.client.post("/auth/logout", cookies={"chat_dada_session": "session-token"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("chat_dada_session=", response.headers.get("set-cookie", ""))


if __name__ == "__main__":
    unittest.main()
