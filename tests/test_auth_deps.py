from __future__ import annotations

import unittest

from fastapi import HTTPException

from apps.web.deps import ensure_owner_or_404, resolve_request_user_id


class _User:
    def __init__(self, user_id: str) -> None:
        self.id = user_id


class AuthDepsTests(unittest.TestCase):
    def test_resolve_request_user_id_prefers_current_user(self) -> None:
        self.assertEqual(resolve_request_user_id(_User("u1"), "anonymous"), "u1")

    def test_resolve_request_user_id_falls_back_to_anonymous(self) -> None:
        self.assertEqual(resolve_request_user_id(None, None), "anonymous")

    def test_resolve_request_user_id_uses_requested_when_unauthenticated(self) -> None:
        self.assertEqual(resolve_request_user_id(None, "legacy_user"), "legacy_user")

    def test_ensure_owner_allows_matching_user(self) -> None:
        ensure_owner_or_404(resource_user_id="u1", current_user=_User("u1"))

    def test_ensure_owner_raises_404_for_other_users(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            ensure_owner_or_404(resource_user_id="u2", current_user=_User("u1"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_ensure_owner_requires_login_for_non_anonymous_resources(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            ensure_owner_or_404(resource_user_id="u2", current_user=None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_ensure_owner_allows_anonymous_resource_without_login(self) -> None:
        ensure_owner_or_404(resource_user_id="anonymous", current_user=None)


if __name__ == "__main__":
    unittest.main()
