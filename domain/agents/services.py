from __future__ import annotations

from task_platform.domain_registry import registry as domain_registry


def get_registered_domain_runner(name: str):
    return domain_registry.get(name)


def list_registered_domains() -> str:
    return domain_registry.summary()
