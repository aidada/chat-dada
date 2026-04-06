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


def auto_discover() -> None:
    """Scan agent/domains/*/orchestrated.py and register any AgentProtocol subclasses.

    Falls back to the legacy static imports when an ``orchestrated`` module does
    not expose an AgentProtocol subclass.
    """
    import importlib
    import logging
    import pathlib

    try:
        from agent.domains._base.protocol import AgentProtocol as _AP
    except ImportError:
        _AP = None  # type: ignore[assignment]

    _log = logging.getLogger("chatdada.domain_registry")
    agents_root = pathlib.Path(__file__).resolve().parent.parent / "domains"

    for child in sorted(agents_root.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        mod_path = child / "orchestrated.py"
        if not mod_path.exists():
            continue
        module_name = f"agent.domains.{child.name}.orchestrated"
        try:
            mod = importlib.import_module(module_name)
        except Exception:
            _log.exception("Failed to import %s", module_name)
            continue

        # Prefer AgentProtocol subclass auto-registration
        registered_via_protocol = False
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if _AP is not None and isinstance(obj, type) and issubclass(obj, _AP) and obj is not _AP and hasattr(obj, "manifest"):
                obj.register()
                _log.info("Auto-registered agent: %s", obj.manifest.name)
                registered_via_protocol = True

        if registered_via_protocol:
            continue

        # Fallback: look for legacy run_*_domain_orchestrated function
        for attr_name in dir(mod):
            if attr_name.startswith("run_") and attr_name.endswith("_domain_orchestrated"):
                runner = getattr(mod, attr_name)
                domain = child.name
                if not registry.is_registered(domain):
                    registry.register(domain, runner, aliases=[domain])
                    _log.info("Legacy-registered domain: %s", domain)


auto_discover()
