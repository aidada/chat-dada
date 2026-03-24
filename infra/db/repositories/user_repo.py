from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.models.oauth_account import OAuthAccount
from infra.db.models.user import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: str) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_google_sub(self, google_sub: str) -> User | None:
        stmt = (
            select(User)
            .join(OAuthAccount, OAuthAccount.user_id == User.id)
            .where(OAuthAccount.provider == "google", OAuthAccount.provider_user_id == google_sub)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_user(
        self,
        *,
        email: str,
        display_name: str,
        password_hash: str | None = None,
        email_verified: bool = False,
        avatar_url: str = "",
    ) -> User:
        user = User(
            email=email,
            display_name=display_name,
            password_hash=password_hash,
            email_verified=email_verified,
            avatar_url=avatar_url,
        )
        self.session.add(user)
        await self.session.flush()
        return user

    async def bind_google_account(
        self,
        *,
        user_id: str,
        provider_user_id: str,
        provider_email: str,
    ) -> OAuthAccount:
        account = OAuthAccount(
            user_id=user_id,
            provider="google",
            provider_user_id=provider_user_id,
            provider_email=provider_email,
        )
        self.session.add(account)
        await self.session.flush()
        return account
