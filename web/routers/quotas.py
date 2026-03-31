from __future__ import annotations

from fastapi import APIRouter, Depends

from web.deps import get_admin_user, get_current_user, get_quota_service
from domain.billing.schemas import UserQuotaUpdateRequest, UserQuotaView
from domain.billing.services import QuotaService

router = APIRouter(tags=["quotas"])


@router.get("/me/quota", response_model=UserQuotaView)
async def get_my_quota(
    current_user=Depends(get_current_user),
    quota_service: QuotaService = Depends(get_quota_service),
):
    return await quota_service.get_user_quota_view(user_id=current_user.id)


@router.get("/admin/users/{user_id}/quota", response_model=UserQuotaView)
async def get_user_quota(
    user_id: str,
    _admin_user=Depends(get_admin_user),
    quota_service: QuotaService = Depends(get_quota_service),
):
    return await quota_service.get_user_quota_view(user_id=user_id)


@router.put("/admin/users/{user_id}/quota", response_model=UserQuotaView)
async def update_user_quota(
    user_id: str,
    payload: UserQuotaUpdateRequest,
    _admin_user=Depends(get_admin_user),
    quota_service: QuotaService = Depends(get_quota_service),
):
    return await quota_service.upsert_user_quota(user_id=user_id, payload=payload)
