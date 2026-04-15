from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from web.config import settings


def _to_async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


ASYNC_DATABASE_URL = _to_async_database_url(settings.database_url)
engine: AsyncEngine = create_async_engine(ASYNC_DATABASE_URL, future=True, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
