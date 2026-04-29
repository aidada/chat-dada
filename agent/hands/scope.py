"""ToolScope — 工具执行的权限边界定义。

每个 Tool 在注册时声明其能力范围。PolicyResolver 根据用户/租户/环境
计算实际授权。ToolGateway 在每次 execute() 时校验 scope。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolScope:
    """工具的能力范围和权限约束。

    name + capability 用于路由和匹配。
    resource_pattern / write_scope 用于资源级权限校验。
    approval_required / rate_limit 用于运行时控制。
    """

    name: str
    capability: str = ""                    # "web.search", "file.read", "office.write"
    description: str = ""
    resource_pattern: str = ""              # "web.search:*", "file.read:workspace/**"
    write_scope: str | None = None          # "office.write:task_dir_only"
    approval_required: bool = False
    approval_gate: str | None = None        # "manager_approval", "quota_check"
    audit_label: str = ""                   # 审计日志标签
    rate_limit: str | None = None           # "10/minute", "1000/day"

    def matches_capability(self, pattern: str) -> bool:
        """检查 capability 是否匹配给定的 pattern。

        pattern 可以是完整 capability 名称或是带通配符的模式。
        """
        if not self.capability:
            return False
        if pattern == self.capability:
            return True
        if pattern.endswith("*") and self.capability.startswith(pattern[:-1]):
            return True
        return False

    def matches_resource(self, target_path: str) -> bool:
        """检查 resource_pattern 是否覆盖 target_path。"""
        if not self.resource_pattern:
            return True
        pattern = self.resource_pattern
        if pattern == "*":
            return True
        if pattern.endswith("/**"):
            prefix = pattern[:-3]
            return target_path.startswith(prefix)
        if pattern.endswith("/*"):
            prefix = pattern[:-2]
            return (target_path.startswith(prefix) and
                    "/" not in target_path[len(prefix):].rstrip("/"))
        return target_path == pattern


__all__ = ["ToolScope"]
