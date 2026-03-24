from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.models.user_session import UserSession


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_session(
        self,
        *,
        user_id: str,
        session_token_hash: str,
        expires_at: datetime,
        user_agent: str,
        ip_address: str,
    ) -> UserSession:
        row = UserSession(
            user_id=user_id,
            session_token_hash=session_token_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_active_by_token_hash(self, token_hash: str) -> UserSession | None:
        stmt = select(UserSession).where(
            UserSession.session_token_hash == token_hash,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > datetime.now(UTC),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def revoke_session(self, session_row: UserSession) -> None:
        session_row.revoked_at = datetime.now(UTC)
        await self.session.flush()

    async def touch_session(self, session_row: UserSession) -> None:
        session_row.last_seen_at = datetime.now(UTC)
        await self.session.flush()
