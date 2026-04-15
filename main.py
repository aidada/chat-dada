"""FastAPI 入口。

Web 路由和 app 初始化逻辑位于 `web/`，
这里保留为兼容启动入口。
"""

from web.app import app
from web.runtime import task_service

__all__ = ["app", "task_service"]
