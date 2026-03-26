from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.web.deps import (
    get_auth_service,
    get_current_user,
    resolve_current_user_once,
    resolve_current_user_once_with_metadata,
)
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
    def __init__(self, session=None) -> None:
        self.session = session

    async def register_with_password(self, *, email: str, password: str, display_name: str = ""):
        return _FakeUser(email=email, display_name=display_name or "registered")

    async def login_with_password(self, *, email: str, password: str):
        return _FakeUser(email=email, display_name="logged-in")

    async def create_user_session(self, *, user_id: str, user_agent: str, ip_address: str):
        return "session-token", datetime.now(UTC) + timedelta(days=7)

    async def logout_by_session_token(self, token: str) -> None:
        return None

    async def get_user_by_session_token(self, token: str):
        if token == "session-token":
            return _FakeUser(), object()
        return None, None


class _FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


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

    def test_resolve_current_user_once_uses_short_lived_session(self) -> None:
        class _Request:
            cookies = {"chat_dada_session": "session-token"}

        async def _run():
            with patch("apps.web.deps.auth.SessionFactory", return_value=_FakeSessionContext()):
                with patch("apps.web.deps.auth.AuthService", _FakeAuthService):
                    user = await resolve_current_user_once(_Request())
            return user

        import asyncio

        user = asyncio.run(_run())
        self.assertEqual(user.id, "user_1")

    def test_resolve_current_user_once_with_metadata_returns_auth_timing(self) -> None:
        class _Request:
            cookies = {"chat_dada_session": "session-token"}

        async def _run():
            with patch("apps.web.deps.auth.SessionFactory", return_value=_FakeSessionContext()):
                with patch("apps.web.deps.auth.AuthService", _FakeAuthService):
                    return await resolve_current_user_once_with_metadata(_Request())

        import asyncio

        user, metadata = asyncio.run(_run())
        self.assertEqual(user.id, "user_1")
        self.assertIn("auth_lookup_ms", metadata)


if __name__ == "__main__":
    unittest.main()
