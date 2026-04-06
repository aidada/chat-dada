from __future__ import annotations

from agent.coordinator.skills import skill_registry


def get_registered_domain_runner(name: str):
    """Get a domain runner by name. Uses skill_registry."""
    # Convert domain name to skill name format
    skill_name = f"do_{name}" if not name.startswith("do_") else name
    return skill_registry.get_runner(skill_name)


def list_registered_domains() -> str:
    """List registered domains. Uses skill_registry summary."""
    return skill_registry.summary()
