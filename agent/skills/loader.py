"""SkillLoader — Skill 的动态检索与 guidance 加载。

通过 goal 语义匹配检索相关 Skill，生成候选摘要给 LLM 选择，
加载完整 SOP 文本注入 Sub Graph prompt。
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent.skills.registry import SkillDefinition, SkillRegistry

_log = logging.getLogger("chatdada.skills.loader")


class SkillLoader:
    def __init__(self, registry: SkillRegistry, definitions_dir: Path):
        self._registry = registry
        self._dir = definitions_dir

    def reload(self) -> None:
        self._registry.reload()

    def search(
        self,
        goal: str,
        *,
        domain: str | None = None,
        hints: list[str] | None = None,
        top_k: int = 3,
    ) -> list[SkillDefinition]:
        candidates = self._registry.list_active()
        if domain:
            candidates = self._registry.list_by_domain(domain)
        if not candidates:
            return []

        goal_lower = str(goal or "").lower()
        hint_set = set((h or "").lower() for h in (hints or []) if h)

        def _score(skill: SkillDefinition) -> float:
            s = 0.0
            for kw in skill.best_for:
                if kw.lower() in goal_lower:
                    s += 2.0
            desc_lower = skill.description.lower()
            for word in goal_lower.split():
                if len(word) >= 3 and word in desc_lower:
                    s += 0.5
            if hint_set:
                for keyword in skill.best_for:
                    if any(h in keyword.lower() for h in hint_set):
                        s += 3.0
            return s

        scored = [(skill, _score(skill)) for skill in candidates]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [skill for skill, s in scored[:top_k] if s > 0]

    def summarize(self, skills: list[SkillDefinition]) -> str:
        if not skills:
            return "（无可用能力包参考）"
        lines = ["## 可用能力包 (Skills)\n"]
        for skill in skills:
            lines.append(
                f"- **{skill.name}:{skill.version}** — {skill.description}\n"
                f"  适用场景: {', '.join(skill.best_for[:3])}\n"
                f"  需要工具: {', '.join(skill.tools_required)}\n"
                f"  风险等级: {skill.risk_level}"
            )
        return "\n".join(lines)

    def load_guidance(self, key: str) -> str:
        skill = self._registry.get(key)
        if not skill:
            _log.warning("Skill not found: %s", key)
            return ""
        sections = [f"## 参考能力包: {skill.name} (v{skill.version})"]
        if skill.guidance:
            sections.append(skill.guidance)
        if skill.output_format:
            sections.append(f"### 产出格式要求\n{skill.output_format}")
        if skill.quality_gate:
            sections.append(f"### 质量门\n{skill.quality_gate}")
        if skill.constraints:
            sections.append("### 约束\n" + "\n".join(f"- {c}" for c in skill.constraints))
        return "\n\n".join(sections)

    def describe_tools(self, allowed_tools: list) -> str:
        if not allowed_tools:
            return "（无可用工具）"
        lines = ["## 可用工具 (Tools)\n"]
        for scope in allowed_tools:
            name = getattr(scope, "name", str(scope))
            cap = getattr(scope, "capability", "")
            desc = getattr(scope, "description", "")
            lines.append(f"- **{name}** ({cap if cap else 'general'})" +
                        (f": {desc}" if desc else ""))
        return "\n".join(lines)


__all__ = ["SkillLoader"]
