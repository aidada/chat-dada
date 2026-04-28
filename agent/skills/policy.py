"""PolicyResolver — 代码侧硬边界，根据用户/租户/环境计算可用 Tool 和执行限制。

这是工具授权链路中唯一的裁决点。LLM 只能在裁决结果内选择工具。
Skills 由 SkillLoader 基于 goal 动态检索，不经过 PolicyResolver，也不在策略层维护 Skill 列表。
Tools 的 allowed_tools 是必须执行的硬边界。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.hands.scope import ToolScope


@dataclass
class PolicyContext:
    user_id: str
    user_role: str = "member"           # admin | member | guest
    tenant_id: str = "default"
    task_type: str | None = None
    environment: str = "development"    # development | staging | production
    quota_daily_tokens_remaining: int = 100_000


@dataclass
class ResolvedPolicy:
    allowed_tools: list[ToolScope]
    max_iterations: int
    max_parallel_agents: int
    require_approval_for: list[str]     # capability patterns needing approval


class PolicyResolver:
    """根据用户角色、租户策略和环境计算可用 Tool 范围。

    Skills 不在这里裁决；SkillLoader 只按 status/domain/environment/goal 动态检索。
    Tools 的 allowed_tools 是必须执行的硬边界。
    """

    def __init__(self, tool_scopes: list[ToolScope] | None = None):
        self._tool_scopes: dict[str, ToolScope] = {}
        if tool_scopes:
            for scope in tool_scopes:
                self._tool_scopes[scope.name] = scope

    def register_tool(self, scope: ToolScope) -> None:
        self._tool_scopes[scope.name] = scope

    def resolve(self, ctx: PolicyContext) -> ResolvedPolicy:
        allowed_tools = self._resolve_tools(ctx)
        max_iterations = self._resolve_max_iterations(ctx)
        max_parallel = self._resolve_max_parallel(ctx)
        approval = self._resolve_approval_rules(ctx)

        return ResolvedPolicy(
            allowed_tools=allowed_tools,
            max_iterations=max_iterations,
            max_parallel_agents=max_parallel,
            require_approval_for=approval,
        )

    def _resolve_tools(self, ctx: PolicyContext) -> list[ToolScope]:
        result: list[ToolScope] = []
        for name, scope in self._tool_scopes.items():
            resolved = ToolScope(
                name=scope.name,
                capability=scope.capability,
                description=scope.description,
                resource_pattern=scope.resource_pattern,
                write_scope=scope.write_scope,
                approval_required=scope.approval_required,
                approval_gate=scope.approval_gate,
                audit_label=scope.audit_label,
                rate_limit=scope.rate_limit,
            )
            if ctx.environment == "production" and resolved.write_scope:
                resolved.approval_required = True
            if ctx.user_role == "guest":
                resolved.write_scope = None
                resolved.approval_required = True
            result.append(resolved)
        return result

    def _resolve_max_iterations(self, ctx: PolicyContext) -> int:
        base = {"admin": 50, "member": 20, "guest": 5}.get(ctx.user_role, 20)
        if ctx.quota_daily_tokens_remaining < 10_000:
            base = min(base, 5)
        return base

    def _resolve_max_parallel(self, ctx: PolicyContext) -> int:
        return {"admin": 10, "member": 5, "guest": 1}.get(ctx.user_role, 3)

    def _resolve_approval_rules(self, ctx: PolicyContext) -> list[str]:
        rules: list[str] = []
        if ctx.environment == "production":
            rules.extend(["file.write", "office.write", "db.write"])
        if ctx.user_role == "guest":
            rules.append("web.search")
        return rules


__all__ = ["PolicyContext", "ResolvedPolicy", "PolicyResolver"]
