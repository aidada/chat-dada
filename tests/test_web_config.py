from __future__ import annotations

import os
import unittest

from web.config import WebSettings


class WebConfigTests(unittest.TestCase):
    def test_cors_allowed_origins_auto_include_tauri_origins(self) -> None:
        original = os.environ.get("CORS_ALLOWED_ORIGINS")
        try:
            os.environ["CORS_ALLOWED_ORIGINS"] = "http://127.0.0.1:5173,http://localhost:5173"
            settings = WebSettings()
            self.assertIn("tauri://localhost", settings.cors_allowed_origins)
            self.assertIn("http://tauri.localhost", settings.cors_allowed_origins)
            self.assertIn("https://tauri.localhost", settings.cors_allowed_origins)
        finally:
            if original is None:
                os.environ.pop("CORS_ALLOWED_ORIGINS", None)
            else:
                os.environ["CORS_ALLOWED_ORIGINS"] = original
