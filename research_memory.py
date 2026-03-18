"""Compatibility shim — real module at capabilities/memory.py"""
import capabilities.memory as _real  # noqa: F401
from capabilities.memory import *  # noqa: F401,F403


def __getattr__(name):  # noqa: N807
    return getattr(_real, name)
