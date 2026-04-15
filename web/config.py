from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import cached_property
from urllib.parse import urlparse

from dotenv import load_dotenv


load_dotenv()

_DESKTOP_ALLOWED_ORIGINS = (
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
)


@dataclass(frozen=True)
class WebSettings:
    app_name: str = os.environ.get("APP_NAME", "Local Agent")
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql://chatdada:chatdada@localhost:5432/chatdada"
    )
    redis_url: str = os.environ.get("REDIS_URL", "redis://localhost:6379")
    frontend_dist_dir: str = os.environ.get("FRONTEND_DIST_DIR", "")
    session_cookie_name: str = os.environ.get("SESSION_COOKIE_NAME", "chat_dada_session")
    app_session_secret: str = os.environ.get("APP_SESSION_SECRET", "dev-session-secret-change-me")
    session_max_age_seconds: int = int(os.environ.get("SESSION_MAX_AGE_SECONDS", str(60 * 60 * 24 * 30)))
    session_secure: bool = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
    session_domain: str = os.environ.get("SESSION_COOKIE_DOMAIN", "")
    session_same_site: str = os.environ.get("SESSION_COOKIE_SAMESITE", "lax")
    app_base_url: str = os.environ.get("APP_BASE_URL", "http://127.0.0.1:8000")
    frontend_redirect_url: str = os.environ.get("FRONTEND_REDIRECT_URL", "/")
    cors_allowed_origins_raw: str = os.environ.get("CORS_ALLOWED_ORIGINS", "*")
    google_client_id: str = os.environ.get("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    google_server_metadata_url: str = os.environ.get(
        "GOOGLE_SERVER_METADATA_URL",
        "https://accounts.google.com/.well-known/openid-configuration",
    )
    google_callback_url: str = os.environ.get("GOOGLE_CALLBACK_URL", "")
    admin_emails_raw: str = os.environ.get("ADMIN_EMAILS", "")
    model_pricing_json: str = os.environ.get("MODEL_PRICING_JSON", "")

    @cached_property
    def cors_allowed_origins(self) -> list[str]:
        raw = self.cors_allowed_origins_raw.strip()
        if not raw:
            return list(_DESKTOP_ALLOWED_ORIGINS)
        if raw == "*":
            return ["*"]
        origins = [item.strip() for item in raw.split(",") if item.strip()]
        for origin in _DESKTOP_ALLOWED_ORIGINS:
            if origin not in origins:
                origins.append(origin)
        return origins

    @cached_property
    def effective_google_callback_url(self) -> str:
        if self.google_callback_url:
            return self.google_callback_url
        return f"{self.app_base_url.rstrip('/')}/auth/google/callback"

    @cached_property
    def admin_emails(self) -> set[str]:
        return {
            item.strip().lower()
            for item in self.admin_emails_raw.split(",")
            if item.strip()
        }

    @cached_property
    def model_pricing(self) -> dict[str, dict[str, float]]:
        raw = self.model_pricing_json.strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        normalized: dict[str, dict[str, float]] = {}
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            normalized[key] = {
                str(metric): float(amount)
                for metric, amount in value.items()
                if isinstance(metric, str) and isinstance(amount, (int, float))
            }
        return normalized

    @cached_property
    def startup_warnings(self) -> list[str]:
        warnings: list[str] = []
        if not self.google_client_id or not self.google_client_secret:
            warnings.append("Google OAuth 未完整配置：缺少 GOOGLE_CLIENT_ID 或 GOOGLE_CLIENT_SECRET。")
        if self.session_secure and not self.app_base_url.startswith("https://"):
            warnings.append("SESSION_COOKIE_SECURE=true 但 APP_BASE_URL 不是 https，回调和 cookie 可能失效。")
        if not self.session_secure and self.app_base_url.startswith("https://"):
            warnings.append("当前 APP_BASE_URL 是 https，但 SESSION_COOKIE_SECURE=false；生产环境应改为 true。")
        if self.cors_allowed_origins == ["*"] and self.session_secure:
            warnings.append("生产环境不建议在携带 cookie 的情况下使用 CORS_ALLOWED_ORIGINS=*。")
        if self.frontend_redirect_url and self.frontend_redirect_url.startswith("http"):
            app_host = urlparse(self.app_base_url).hostname or ""
            frontend_host = urlparse(self.frontend_redirect_url).hostname or ""
            if app_host and frontend_host and app_host != frontend_host:
                warnings.append("FRONTEND_REDIRECT_URL 指向与 APP_BASE_URL 不同的域名，请确认 cookie domain 和 CORS 设置。")
        if self.app_session_secret == "dev-session-secret-change-me":
            warnings.append("APP_SESSION_SECRET 仍是默认开发值，生产环境必须替换。")
        if self.model_pricing_json and not self.model_pricing:
            warnings.append("MODEL_PRICING_JSON 解析失败，cost_usd 将退化为 0。")
        return warnings


settings = WebSettings()
