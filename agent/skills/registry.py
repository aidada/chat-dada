"""SkillRegistry — Skill 的注册与查询。

Skills 是 SOP 文本、领域知识和约束规则。
通过 name:version 唯一标识，支持按 domain 过滤和关键词匹配。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger("chatdada.skills.registry")


@dataclass
class SkillDefinition:
    name: str
    version: str = "v1"
    status: str = "active"                    # active | deprecated | draft
    checksum: str = ""
    owner: str = ""

    description: str = ""
    best_for: list[str] = field(default_factory=list)
    compatible_domains: list[str] = field(default_factory=list)

    guidance: str = ""                        # SOP 文本，注入 prompt
    input_contract: dict = field(default_factory=dict)
    output_contract: dict = field(default_factory=dict)
    output_format: str = ""

    tools_required: list[str] = field(default_factory=list)
    tools_optional: list[str] = field(default_factory=list)

    risk_level: str = "low"                   # low | medium | high
    quality_gate: str = ""
    constraints: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> "SkillDefinition | None":
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            _log.warning("Failed to read skill file %s: %s", path, exc)
            return None
        try:
            raw = json.loads(raw_bytes)
        except json.JSONDecodeError as exc:
            _log.warning("Failed to parse skill file %s: %s", path, exc)
            return None

        raw["checksum"] = hashlib.sha256(raw_bytes).hexdigest()

        return cls(
            name=raw.get("name", path.stem.split(".")[0]),
            version=raw.get("version", "v1"),
            status=raw.get("status", "active"),
            checksum=raw.get("checksum", ""),
            owner=raw.get("owner", ""),
            description=raw.get("description", ""),
            best_for=raw.get("best_for", []),
            compatible_domains=raw.get("compatible_domains", []),
            guidance=raw.get("guidance", ""),
            input_contract=raw.get("input_contract", {}),
            output_contract=raw.get("output_contract", {}),
            output_format=raw.get("output_format", ""),
            tools_required=raw.get("tools_required", []),
            tools_optional=raw.get("tools_optional", []),
            risk_level=raw.get("risk_level", "low"),
            quality_gate=raw.get("quality_gate", ""),
            constraints=raw.get("constraints", []),
        )


class SkillRegistry:
    """Skills 的中心注册表。

    从文件目录加载 .skill.json 文件，提供按 name:version 查询能力。
    """

    def __init__(self, definitions_dir: Path | None = None):
        self._skills: dict[str, SkillDefinition] = {}
        self._dir = definitions_dir

    def reload(self) -> None:
        if not self._dir or not self._dir.exists():
            return
        self._skills.clear()
        for path in self._dir.glob("*.skill.json"):
            if not path.is_file():
                continue
            skill = SkillDefinition.from_file(path)
            if skill is None:
                continue
            key = f"{skill.name}:{skill.version}"
            self._skills[key] = skill
        _log.info("Loaded %d skills from %s", len(self._skills), self._dir)

    def get(self, key: str) -> SkillDefinition | None:
        return self._skills.get(key)

    def list_active(self) -> list[SkillDefinition]:
        return [s for s in self._skills.values() if s.status == "active"]

    def list_by_domain(self, domain: str) -> list[SkillDefinition]:
        return [
            s for s in self.list_active()
            if domain in s.compatible_domains or "*" in s.compatible_domains
        ]

    def __len__(self) -> int:
        return len(self._skills)


__all__ = ["SkillDefinition", "SkillRegistry"]
