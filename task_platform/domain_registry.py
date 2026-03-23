from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

DomainRunner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class DomainRegistry:
    def __init__(self) -> None:
        self._runners: dict[str, DomainRunner] = {}
        self._aliases: dict[str, str] = {}

    def register(self, name: str, runner: DomainRunner, aliases: list[str] | None = None) -> None:
        self._runners[name] = runner
        self._aliases[name.lower()] = name
        for alias in aliases or []:
            self._aliases[alias.lower()] = name

    def get(self, name: str) -> DomainRunner | None:
        canonical = self._aliases.get(name.lower(), name)
        return self._runners.get(canonical)

    def is_registered(self, name: str) -> bool:
        return self.get(name) is not None

    def resolve_alias(self, name: str) -> str | None:
        return self._aliases.get(name.lower())

    def summary(self) -> str:
        lines = []
        for alias, canonical in sorted(self._aliases.items()):
            if alias == canonical:
                lines.append(f"- {canonical}")
            else:
                lines.append(f"- {alias} -> {canonical}")
        return "\n".join(lines)


registry = DomainRegistry()

# Register known domains — all using orchestrated versions
from domain_agents.patent.orchestrated import run_patent_domain_orchestrated  # noqa: E402
from domain_agents.ppt.orchestrated import run_ppt_domain_orchestrated  # noqa: E402
from domain_agents.research.orchestrated import run_research_domain_orchestrated  # noqa: E402
from domain_agents.zero_report.orchestrated import run_zero_report_domain_orchestrated  # noqa: E402

registry.register("research", run_research_domain_orchestrated, aliases=["deep_research", "research"])
registry.register("patent", run_patent_domain_orchestrated, aliases=["patent", "专利"])
registry.register("zero_report", run_zero_report_domain_orchestrated, aliases=["zero_report", "zero report", "postmortem", "归零"])
registry.register("ppt", run_ppt_domain_orchestrated, aliases=["ppt", "幻灯片", "powerpoint"])
