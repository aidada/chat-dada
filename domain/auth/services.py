from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from apps.web.config import settings
from domain.auth.password import hash_password, verify_password
from infra.db.repositories.session_repo import SessionRepository
from infra.db.repositories.user_repo import UserRepository


def _normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def _display_name_from_email(email: str) -> str:
    local = _normalize_email(email).split("@", 1)[0]
    return local[:40] or "user"


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.sessions = SessionRepository(session)

    async def register_with_password(
        self,
        *,
        email: str,
        password: str,
        display_name: str = "",
    ):
        normalized_email = _normalize_email(email)
        existing = await self.users.get_by_email(normalized_email)
        if existing is not None:
            raise ValueError("该邮箱已注册")

        user = await self.users.create_user(
            email=normalized_email,
            display_name=display_name.strip() or _display_name_from_email(normalized_email),
            password_hash=hash_password(password),
            email_verified=False,
        )
        await self.session.commit()
        return user

    async def login_with_password(self, *, email: str, password: str):
        normalized_email = _normalize_email(email)
        user = await self.users.get_by_email(normalized_email)
        if user is None or not user.password_hash:
            raise ValueError("邮箱或密码错误")
        if not verify_password(password, user.password_hash):
            raise ValueError("邮箱或密码错误")
        user.last_login_at = datetime.now(UTC)
        await self.session.commit()
        return user

    async def login_with_google(
        self,
        *,
        email: str,
        email_verified: bool,
        provider_user_id: str,
        display_name: str,
        avatar_url: str,
    ):
        user = await self.users.get_by_google_sub(provider_user_id)
        if user is None:
            existing_user = await self.users.get_by_email(_normalize_email(email))
            if existing_user is not None:
                user = existing_user
            else:
                user = await self.users.create_user(
                    email=_normalize_email(email),
                    display_name=display_name.strip() or _display_name_from_email(email),
                    email_verified=email_verified,
                    avatar_url=avatar_url,
                )
            await self.users.bind_google_account(
                user_id=user.id,
                provider_user_id=provider_user_id,
                provider_email=_normalize_email(email),
            )

        user.email_verified = user.email_verified or email_verified
        if avatar_url and not user.avatar_url:
            user.avatar_url = avatar_url
        if display_name and not user.display_name:
            user.display_name = display_name
        user.last_login_at = datetime.now(UTC)
        await self.session.commit()
        return user

    async def create_user_session(self, *, user_id: str, user_agent: str, ip_address: str) -> tuple[str, datetime]:
        raw_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=settings.session_max_age_seconds)
        await self.sessions.create_session(
            user_id=user_id,
            session_token_hash=_hash_session_token(raw_token),
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        await self.session.commit()
        return raw_token, expires_at

    async def get_user_by_session_token(self, token: str):
        token_hash = _hash_session_token(token)
        session_row = await self.sessions.get_active_by_token_hash(token_hash)
        if session_row is None:
            return None, None
        await self.sessions.touch_session(session_row)
        user = await self.users.get_by_id(session_row.user_id)
        await self.session.commit()
        return user, session_row

    async def logout_by_session_token(self, token: str) -> None:
        token_hash = _hash_session_token(token)
        session_row = await self.sessions.get_active_by_token_hash(token_hash)
        if session_row is None:
            return
        await self.sessions.revoke_session(session_row)
        await self.session.commit()
