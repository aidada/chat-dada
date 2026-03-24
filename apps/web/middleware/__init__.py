"""Web middleware 组合入口。"""

from apps.web.middleware.errors import register_exception_handlers
from apps.web.middleware.sessions import install_session_middleware

__all__ = [
    "install_session_middleware",
    "register_exception_handlers",
]
