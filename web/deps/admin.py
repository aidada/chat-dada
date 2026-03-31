from __future__ import annotations

from fastapi import Depends, HTTPException

from apps.web.config import settings
from apps.web.deps.auth import get_current_user


async def get_admin_user(current_user=Depends(get_current_user)):
    if not settings.admin_emails:
        raise HTTPException(status_code=403, detail="尚未配置管理员邮箱")
    if str(getattr(current_user, "email", "") or "").strip().lower() not in settings.admin_emails:
        raise HTTPException(status_code=403, detail="当前账号没有管理员权限")
    return current_user
