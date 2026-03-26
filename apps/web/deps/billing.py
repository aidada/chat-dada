from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from domain.billing.services import QuotaService
from infra.db.repositories.quota_repo import UsageEventRepository, UserQuotaRepository
from infra.db.session import get_db_session


async def get_quota_service(session: AsyncSession = Depends(get_db_session)) -> QuotaService:
    return QuotaService(UserQuotaRepository(session), UsageEventRepository(session))
