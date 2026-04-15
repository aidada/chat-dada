from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.deps import (
    get_auth_service,
    get_current_user,
    resolve_current_user_once,
    resolve_current_user_once_with_metadata,
)
from web.routers.auth import (
    _desktop_handoffs,
    _desktop_tickets,
    _oauth_redirect_targets,
    _resolve_redirect_target,
    router as auth_router,
)


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

    async def login_with_google(
        self,
        *,
        email: str,
        email_verified: bool,
        provider_user_id: str,
        display_name: str,
        avatar_url: str,
    ):
        return _FakeUser(
            email=email,
            email_verified=email_verified,
            display_name=display_name or "oauth-user",
            avatar_url=avatar_url,
        )

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
        _desktop_handoffs.clear()
        _desktop_tickets.clear()
        _oauth_redirect_targets.clear()
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

    def test_me_accepts_header_session_token(self) -> None:
        response = self.client.get("/auth/me", headers={"X-Session-Token": "session-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["id"], "user_1")

    def test_logout_clears_cookie(self) -> None:
        response = self.client.post("/auth/logout", cookies={"chat_dada_session": "session-token"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("chat_dada_session=", response.headers.get("set-cookie", ""))

    def test_resolve_redirect_target_allows_tauri_localhost(self) -> None:
        self.assertEqual(
            _resolve_redirect_target("tauri://localhost/auth-complete"),
            "tauri://localhost/auth-complete",
        )

    def test_resolve_redirect_target_rejects_unknown_host(self) -> None:
        self.assertEqual(
            _resolve_redirect_target("https://evil.example.com/hijack"),
            "http://127.0.0.1:5173",
        )

    def test_desktop_auth_poll_pending_by_default(self) -> None:
        response = self.client.get("/auth/desktop/poll", params={"flow_id": "missing"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending")

    def test_desktop_auth_consume_sets_session_cookie(self) -> None:
        _desktop_handoffs["flow-1"] = {
            "ticket": "ticket-1",
            "session_token": "session-token",
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            "user_view": {
                "id": "user_1",
                "email": "u@example.com",
                "email_verified": True,
                "display_name": "User One",
                "avatar_url": "",
            },
        }
        _desktop_tickets["ticket-1"] = "flow-1"

        response = self.client.post("/auth/desktop/consume", json={"ticket": "ticket-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["id"], "user_1")
        self.assertIn("chat_dada_session=session-token", response.headers.get("set-cookie", ""))
        self.assertEqual(_desktop_handoffs, {})
        self.assertEqual(_desktop_tickets, {})

    def test_google_callback_records_desktop_handoff(self) -> None:
        fake_client = type(
            "FakeClient",
            (),
            {
                "authorize_access_token": AsyncMock(return_value={"access_token": "token"}),
                "parse_id_token": AsyncMock(return_value={
                    "email": "oauth@example.com",
                    "email_verified": True,
                    "sub": "google-sub-1",
                    "name": "OAuth User",
                    "picture": "https://example.com/avatar.png",
                }),
            },
        )()
        callback_target = "http://127.0.0.1:8000/auth/desktop/completed?flow_id=flow-2"
        _oauth_redirect_targets["state-123"] = {
            "redirect_to": callback_target,
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        }

        with patch("web.routers.auth.get_google_client", return_value=fake_client):
            response = self.client.get(
                "/auth/google/callback",
                params={"state": "state-123"},
                cookies={
                    "oauth_state": "state-123",
                    "oauth_nonce": "nonce-123",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], callback_target)
        self.assertIn("flow-2", _desktop_handoffs)
        ticket = _desktop_handoffs["flow-2"]["ticket"]
        self.assertEqual(_desktop_tickets[ticket], "flow-2")

    def test_resolve_current_user_once_uses_short_lived_session(self) -> None:
        class _Request:
            cookies = {"chat_dada_session": "session-token"}

        async def _run():
            with patch("web.deps.auth.SessionFactory", return_value=_FakeSessionContext()):
                with patch("web.deps.auth.AuthService", _FakeAuthService):
                    user = await resolve_current_user_once(_Request())
            return user

        import asyncio

        user = asyncio.run(_run())
        self.assertEqual(user.id, "user_1")

    def test_resolve_current_user_once_with_metadata_returns_auth_timing(self) -> None:
        class _Request:
            cookies = {"chat_dada_session": "session-token"}

        async def _run():
            with patch("web.deps.auth.SessionFactory", return_value=_FakeSessionContext()):
                with patch("web.deps.auth.AuthService", _FakeAuthService):
                    return await resolve_current_user_once_with_metadata(_Request())

        import asyncio

        user, metadata = asyncio.run(_run())
        self.assertEqual(user.id, "user_1")
        self.assertIn("auth_lookup_ms", metadata)


if __name__ == "__main__":
    unittest.main()
