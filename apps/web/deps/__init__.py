"""Web 依赖注入入口。"""

from apps.web.deps.admin import get_admin_user
from apps.web.deps.auth import (
    ensure_owner_or_404,
    get_auth_service,
    get_current_user,
    get_optional_current_user,
    resolve_current_user_once,
    resolve_request_user_id,
)
from apps.web.deps.billing import get_quota_service
from apps.web.deps.services import get_conversation_service, get_task_execution_service

__all__ = [
    "get_admin_user",
    "ensure_owner_or_404",
    "get_auth_service",
    "get_current_user",
    "get_optional_current_user",
    "get_conversation_service",
    "get_quota_service",
    "resolve_current_user_once",
    "resolve_request_user_id",
    "get_task_execution_service",
]
