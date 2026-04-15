"""Web 依赖注入入口。"""

from web.deps.admin import get_admin_user
from web.deps.auth import (
    ensure_owner_or_404,
    get_auth_service,
    get_current_user,
    get_optional_current_user,
    resolve_current_user_once,
    resolve_current_user_once_with_metadata,
    resolve_request_user_id,
)
from web.deps.billing import get_quota_service
from web.deps.services import get_conversation_service, get_task_execution_service

__all__ = [
    "get_admin_user",
    "ensure_owner_or_404",
    "get_auth_service",
    "get_current_user",
    "get_optional_current_user",
    "get_conversation_service",
    "get_quota_service",
    "resolve_current_user_once",
    "resolve_current_user_once_with_metadata",
    "resolve_request_user_id",
    "get_task_execution_service",
]
