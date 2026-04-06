"""
Coordinator Skills Registry and Adapter.

This module provides:
- SkillDescription: Standardized skill metadata for LLM selection
- SkillRegistry: Central registry for domain skills and tools
- discover_skills(): Auto-discovery of domain skills (migrated from domain_registry)
- run_skill_via_adapter(): Bridge layer for skill execution
- _make_skill_interrupt_bridge(): Interrupt handling for skill invocations
"""
from __future__ import annotations

import importlib
import logging
import pathlib
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agent.coordinator.state import SkillContext, SkillResult

_log = logging.getLogger("chatdada.coordinator.skills")

# Type alias for skill runner functions
SkillRunner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class SkillDescription:
    """Standardized skill description for Coordinator LLM to understand when to invoke.

    As defined in PRD §3.1.
    """
    name: str  # e.g., "do_research"
    version: str = "1.0"  # Skill version for tracking upgrades
    description: str = ""  # Human-readable description
    input_schema: dict[str, Any] = field(default_factory=dict)  # Expected input fields
    output_schema: dict[str, Any] = field(default_factory=dict)  # Expected output fields
    best_for: list[str] = field(default_factory=list)  # Use cases this skill is best for
    timeout_seconds: int = 300  # Default timeout
    retryable: bool = True  # Whether retry is allowed
    nested_depth_limit: int = 0  # Max nested call depth, 0 = no nesting allowed


class SkillRegistry:
    """Central registry for domain skills and basic tools.

    Replaces the legacy DomainRegistry with a richer SkillDescription model.
    Provides methods for skill lookup, listing, and LLM-friendly summaries.
    """

    def __init__(self) -> None:
        self._skills: dict[str, SkillDescription] = {}
        self._runners: dict[str, SkillRunner] = {}
        self._aliases: dict[str, str] = {}  # lowercase alias -> canonical name

    def register(
        self,
        name: str,
        runner: SkillRunner,
        description: SkillDescription | None = None,
        aliases: list[str] | None = None,
    ) -> None:
        """Register a skill with its runner and description."""
        # Use default description if not provided
        if description is None:
            description = SkillDescription(
                name=name,
                description=f"Skill: {name}",
            )

        self._skills[name] = description
        self._runners[name] = runner

        # Register aliases (case-insensitive)
        self._aliases[name.lower()] = name
        for alias in aliases or []:
            self._aliases[alias.lower()] = name

        _log.info("Registered skill: %s", name)

    def get_runner(self, name: str) -> SkillRunner | None:
        """Get the runner for a skill by name or alias."""
        canonical = self._aliases.get(name.lower(), name)
        return self._runners.get(canonical)

    def get_description(self, name: str) -> SkillDescription | None:
        """Get the skill description by name or alias."""
        canonical = self._aliases.get(name.lower(), name)
        return self._skills.get(canonical)

    def is_registered(self, name: str) -> bool:
        """Check if a skill is registered."""
        return self.get_runner(name) is not None

    def list_skills(self) -> list[SkillDescription]:
        """Return all registered skill descriptions."""
        return list(self._skills.values())

    def skill_summary_for_llm(self) -> str:
        """Generate a text summary of all skills for LLM context.

        Format is optimized for LLM to understand skill capabilities.
        """
        lines = ["## Available Skills\n"]
        for skill in self._skills.values():
            lines.append(f"- **{skill.name}** (v{skill.version}): {skill.description}")
            if skill.best_for:
                lines.append(f"  Best for: {', '.join(skill.best_for)}")
            if skill.input_schema:
                input_fields = list(skill.input_schema.keys())[:5]  # Limit to 5 fields
                lines.append(f"  Input: {', '.join(input_fields)}")

        return "\n".join(lines)

    def resolve_alias(self, name: str) -> str | None:
        """Resolve a skill alias to its canonical name."""
        return self._aliases.get(name.lower())

    def summary(self) -> str:
        """Generate a summary of registered skills and aliases."""
        lines = []
        for alias, canonical in sorted(self._aliases.items()):
            if alias == canonical:
                lines.append(f"- {canonical}")
            else:
                lines.append(f"- {alias} -> {canonical}")
        return "\n".join(lines)


# Global skill registry instance
skill_registry = SkillRegistry()


def discover_skills() -> None:
    """Scan agent/domains/*/orchestrated.py and register domain skills.

    Migrated from agent.platform.domain_registry.auto_discover().

    Falls back to legacy static imports when an orchestrated module does not
    expose an AgentProtocol subclass.
    """
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

        # Look for AgentProtocol subclass auto-registration (preferred)
        # Note: AgentProtocol is defined in deepagents, check if available
        registered_via_protocol = False
        try:
            from deepagents import AgentProtocol as _AP

            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if isinstance(obj, type) and issubclass(obj, _AP) and obj is not _AP and hasattr(obj, "manifest"):
                    obj.register()
                    _log.info("Auto-registered agent via protocol: %s", obj.manifest.name)
                    registered_via_protocol = True
        except ImportError:
            # deepagents not available, skip protocol-based registration
            pass

        if registered_via_protocol:
            continue

        # Fallback: Look for legacy run_*_domain_orchestrated function
        domain_name = child.name
        skill_name = f"do_{domain_name}"  # e.g., do_research, do_patent

        for attr_name in dir(mod):
            if attr_name.startswith("run_") and attr_name.endswith("_domain_orchestrated"):
                runner = getattr(mod, attr_name)
                if callable(runner) and not skill_registry.is_registered(skill_name):
                    # Create skill description based on domain
                    description = _create_skill_description(domain_name)
                    skill_registry.register(
                        skill_name,
                        runner,
                        description=description,
                        aliases=[domain_name, skill_name],
                    )
                    _log.info("Legacy-registered domain skill: %s", skill_name)


def _create_skill_description(domain_name: str) -> SkillDescription:
    """Create a SkillDescription for a domain based on its name.

    Provides reasonable defaults for known domains.
    """
    # Known domain descriptions
    domain_configs: dict[str, dict[str, Any]] = {
        "research": {
            "description": "Execute deep research workflow, suitable for complex multi-dimensional analysis",
            "best_for": ["deep research", "literature review", "technical survey", "comparative analysis"],
            "timeout_seconds": 600,
        },
        "patent": {
            "description": "Generate patent drafts based on technical content",
            "best_for": ["patent drafting", "IP documentation", "invention disclosure"],
            "timeout_seconds": 300,
        },
        "ppt": {
            "description": "Create PowerPoint presentations via OfficeCLI",
            "best_for": ["presentation creation", "slide generation", "report visualization"],
            "timeout_seconds": 300,
        },
        "zero_report": {
            "description": "Generate zero-report documents with planning and review",
            "best_for": ["report writing", "document generation", "structured output"],
            "timeout_seconds": 300,
        },
    }

    config = domain_configs.get(domain_name, {})

    return SkillDescription(
        name=f"do_{domain_name}",
        version="1.0",
        description=config.get("description", f"Execute {domain_name} domain workflow"),
        input_schema={"query": "str: The task query or request"},
        output_schema={"result": "str: The output result", "artifact_refs": "list: Generated artifacts"},
        best_for=config.get("best_for", []),
        timeout_seconds=config.get("timeout_seconds", 300),
        retryable=True,
        nested_depth_limit=0,
    )


# Auto-discover skills on module load
discover_skills()


# ── Skill Adapter and Interrupt Bridge ───────────────────────────────────────


async def run_skill_via_adapter(
    runner: SkillRunner,
    input_data: dict[str, Any],
    context: SkillContext,
) -> SkillResult:
    """Execute a skill via adapter layer, handling interrupt bridging.

    As defined in PRD §6.1 and §6.4.

    This adapter:
    1. Sets up interrupt bridge so skill's ask_user() works correctly
    2. Enriches input_data with coordinator context
    3. Calls the domain runner
    4. Normalizes output to SkillResult format

    IMPORTANT: This function always re-raises GraphInterrupt as an exception;
    it never returns SkillResult(status="interrupted"). This follows PRD §6.4.
    """
    from agent.runtime.interaction import (
        reset_graph_interrupt_bridge,
        set_graph_interrupt_bridge,
    )

    # Setup interrupt bridge
    # PRD §6.4: SkillAdapter must always set up the interrupt bridge
    # If request_interrupt_fn is provided, use it; otherwise create default bridge
    token: Any = None
    if context.request_interrupt_fn is not None:
        # Use the provided interrupt function
        token = set_graph_interrupt_bridge(context.request_interrupt_fn)
    elif context.coordinator_task_id and context.skill_invocation_id:
        # Create default bridge using coordinator context
        bridge = _make_skill_interrupt_bridge(
            context.coordinator_task_id,
            context.skill_invocation_id,
        )
        token = set_graph_interrupt_bridge(bridge)

    try:
        # Enrich input with coordinator context
        enriched_input = {
            **input_data,
            "task_id": context.coordinator_task_id,
            "clarification_history": context.clarification_history,
            "report_profile": context.request_payload.get("report_profile", ""),
            "trace_id": context.trace_id,
        }

        # Execute the skill runner
        result = await runner(enriched_input)

        # Normalize result to SkillResult
        if isinstance(result, SkillResult):
            return result

        # Handle Pydantic model results (domain runners often return Pydantic models)
        if hasattr(result, "model_dump"):
            # Pydantic v2 model
            data = result.model_dump()
            return SkillResult(
                status="ok",
                result=data.get("result") or data.get("final_result") or "",
                artifact_refs=list(data.get("artifact_refs") or []),
                review=dict(data.get("review") or {}),
                budget=dict(data.get("budget") or {}),
                strategy=str(data.get("strategy") or ""),
                latest_checkpoint_id=data.get("latest_checkpoint_id"),
            )

        if hasattr(result, "dict"):
            # Pydantic v1 model
            data = result.dict()
            return SkillResult(
                status="ok",
                result=data.get("result") or data.get("final_result") or "",
                artifact_refs=list(data.get("artifact_refs") or []),
                review=dict(data.get("review") or {}),
                budget=dict(data.get("budget") or {}),
                strategy=str(data.get("strategy") or ""),
                latest_checkpoint_id=data.get("latest_checkpoint_id"),
            )

        # Handle legacy dict return format
        if isinstance(result, dict):
            return SkillResult(
                status="ok",
                result=result.get("result") or result.get("final_result") or "",
                artifact_refs=list(result.get("artifact_refs") or []),
                review=dict(result.get("review") or {}),
                budget=dict(result.get("budget") or {}),
                strategy=str(result.get("strategy") or ""),
                latest_checkpoint_id=result.get("latest_checkpoint_id"),
            )

        # Fallback: treat as raw result
        return SkillResult(
            status="ok",
            result=str(result) if result is not None else "",
        )

    except Exception as exc:
        exc_type = type(exc).__name__
        # Re-raise interrupt exceptions so LangGraph handles them correctly
        if "GraphInterrupt" in exc_type or "Interrupt" in exc_type:
            raise
        _log.exception("Skill execution failed: %s", context.skill_name)
        return SkillResult(
            status="error",
            error=str(exc),
        )

    finally:
        if token is not None:
            reset_graph_interrupt_bridge(token)


def _make_skill_interrupt_bridge(
    coordinator_task_id: str,
    skill_invocation_id: str,
) -> Callable[[dict[str, Any]], str]:
    """Create an interrupt bridge function for skill invocations.

    As defined in PRD §6.4.

    When a skill calls ask_user(payload), this bridge enriches the payload
    with coordinator tracking info and triggers a LangGraph interrupt.
    """
    from agent.platform.interrupts import request_interrupt

    def bridge(payload: dict[str, Any]) -> str:
        enriched = {
            **payload,
            "interrupt_type": payload.get("interrupt_type", "human_input"),
            "coordinator_task_id": coordinator_task_id,
            "skill_invocation_id": skill_invocation_id,
        }
        return request_interrupt(enriched)

    return bridge


# ── Deprecation shim for legacy domain_registry imports ────────────────────────


def _deprecated_registry_access() -> None:
    """Warn users about deprecated domain_registry usage."""
    warnings.warn(
        "domain_registry is deprecated; use agent.coordinator.skills.skill_registry instead.",
        DeprecationWarning,
        stacklevel=3,
    )


__all__ = [
    "SkillDescription",
    "SkillRegistry",
    "skill_registry",
    "discover_skills",
    "run_skill_via_adapter",
    "_make_skill_interrupt_bridge",
    "SkillRunner",
]