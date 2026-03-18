"""Compatibility shim — real module at core/registry.py"""
import core.registry as _real  # noqa: F401
from core.registry import *  # noqa: F401,F403


def __getattr__(name):  # noqa: N807
    return getattr(_real, name)
