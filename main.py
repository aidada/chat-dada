"""FastAPI 入口。

Web 路由和 app 初始化逻辑已经迁到 `apps/web/`，
这里保留为兼容启动入口。
"""

from apps.web.app import app
from apps.web.runtime import task_service

__all__ = ["app", "task_service"]
