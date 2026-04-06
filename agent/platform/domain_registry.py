"""
Domain Registry — DEPRECATED

This module is deprecated. Use agent.coordinator.skills.skill_registry instead.

The global `registry` instance is kept for backward compatibility but delegates
to skill_registry internally. All imports should migrate to:

    from agent.coordinator.skills import skill_registry

This file will be removed in the next release cycle after migration is complete.
"""
from __future__ import annotations

import warnings
from collections.abc import Awaitable, Callable
from typing import Any

DomainRunner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _deprecated() -> None:
    """Emit deprecation warning for domain_registry usage."""
    warnings.warn(
        "domain_registry is deprecated; use agent.coordinator.skills.skill_registry instead. "
        "See PRD §8.3 C4 for migration details.",
        DeprecationWarning,
        stacklevel=3,
    )


class DomainRegistry:
    """
    DEPRECATED: Legacy domain runner registry.

    Use agent.coordinator.skills.SkillRegistry instead.

    This class is preserved for backward compatibility and delegates to
    the new SkillRegistry internally.
    """

    def __init__(self) -> None:
        _deprecated()
        # Internal state kept for backward compatibility with tests that mock this class
        self._runners: dict[str, DomainRunner] = {}
        self._aliases: dict[str, str] = {}

    def register(self, name: str, runner: DomainRunner, aliases: list[str] | None = None) -> None:
        """Register a domain runner. DEPRECATED: Use skill_registry.register()."""
        _deprecated()
        from agent.coordinator.skills import SkillDescription, skill_registry

        # Store in legacy internal state for backward compatibility
        self._runners[name] = runner
        self._aliases[name.lower()] = name
        for alias in aliases or []:
            self._aliases[alias.lower()] = name

        # Delegate to skill_registry
        skill_name = f"do_{name}" if not name.startswith("do_") else name
        description = SkillDescription(
            name=skill_name,
            description=f"Domain: {name}",
        )
        skill_registry.register(skill_name, runner, description=description, aliases=aliases)

    def get(self, name: str) -> DomainRunner | None:
        """Get a domain runner by name. DEPRECATED: Use skill_registry.get_runner()."""
        _deprecated()
        from agent.coordinator.skills import skill_registry

        # First check legacy internal state (for test mocks)
        canonical = self._aliases.get(name.lower(), name)
        if canonical in self._runners:
            return self._runners.get(canonical)

        # Delegate to skill_registry
        skill_name = f"do_{name}" if not name.startswith("do_") else name
        return skill_registry.get_runner(skill_name)

    def is_registered(self, name: str) -> bool:
        """Check if a domain is registered. DEPRECATED: Use skill_registry.is_registered()."""
        _deprecated()
        from agent.coordinator.skills import skill_registry

        # First check legacy internal state (for test mocks)
        canonical = self._aliases.get(name.lower(), name)
        if canonical in self._runners:
            return True

        # Delegate to skill_registry
        skill_name = f"do_{name}" if not name.startswith("do_") else name
        return skill_registry.is_registered(skill_name)

    def resolve_alias(self, name: str) -> str | None:
        """Resolve a domain alias. DEPRECATED: Use skill_registry.resolve_alias()."""
        _deprecated()
        from agent.coordinator.skills import skill_registry

        # First check legacy internal state (for test mocks)
        if name.lower() in self._aliases:
            return self._aliases.get(name.lower())

        # Delegate to skill_registry
        return skill_registry.resolve_alias(name)

    def summary(self) -> str:
        """Generate a summary of registered domains. DEPRECATED: Use skill_registry.summary()."""
        _deprecated()
        from agent.coordinator.skills import skill_registry

        # First check legacy internal state
        if self._aliases:
            lines = []
            for alias, canonical in sorted(self._aliases.items()):
                if alias == canonical:
                    lines.append(f"- {canonical}")
                else:
                    lines.append(f"- {alias} -> {canonical}")
            return "\n".join(lines)

        # Delegate to skill_registry
        return skill_registry.summary()


# Legacy global registry instance — kept for backward compatibility
registry = DomainRegistry()


def auto_discover() -> None:
    """
    DEPRECATED: Legacy auto-discovery function.

    Use agent.coordinator.skills.discover_skills() instead.
    This function delegates to discover_skills() for backward compatibility.
    """
    _deprecated()
    from agent.coordinator.skills import discover_skills

    discover_skills()


# Legacy auto-discovery call — no longer executed at import time
# The new skill_registry auto-discovery happens in skills.py
# auto_discover()  # REMOVED: skill_registry.discover_skills() runs in skills.py