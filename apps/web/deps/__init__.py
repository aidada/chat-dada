"""Web 依赖注入入口。"""

from apps.web.deps.auth import (
    ensure_owner_or_404,
    get_auth_service,
    get_current_user,
    get_optional_current_user,
    resolve_request_user_id,
)
from apps.web.deps.services import get_conversation_service, get_task_execution_service

__all__ = [
    "ensure_owner_or_404",
    "get_auth_service",
    "get_current_user",
    "get_optional_current_user",
    "get_conversation_service",
    "resolve_request_user_id",
    "get_task_execution_service",
]
