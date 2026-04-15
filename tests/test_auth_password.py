from __future__ import annotations

import unittest

from domain.auth.password import hash_password, verify_password


class AuthPasswordTests(unittest.TestCase):
    def test_hash_and_verify_password(self) -> None:
        password_hash = hash_password("secret-123")
        self.assertTrue(password_hash)
        self.assertTrue(verify_password("secret-123", password_hash))
        self.assertFalse(verify_password("wrong-password", password_hash))


if __name__ == "__main__":
    unittest.main()
