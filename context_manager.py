"""Compatibility shim — real module at capabilities/context_manager.py"""
import capabilities.context_manager as _real  # noqa: F401
from capabilities.context_manager import *  # noqa: F401,F403


def __getattr__(name):  # noqa: N807
    return getattr(_real, name)
