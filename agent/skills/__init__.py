"""Skill 层 — 能力包（SOP / 约束 / 输出格式 / 质量门）。

Skills 是领域知识文本，通过 goal 动态检索后注入 Sub Graph prompt。
Skills 不自动授权 Tools；Tools 授权由 PolicyResolver 硬边界决定。
"""

from agent.skills.loader import SkillLoader
from agent.skills.policy import PolicyContext, PolicyResolver, ResolvedPolicy
from agent.skills.registry import SkillDefinition, SkillRegistry

__all__ = [
    "PolicyContext",
    "PolicyResolver",
    "ResolvedPolicy",
    "SkillDefinition",
    "SkillLoader",
    "SkillRegistry",
]
