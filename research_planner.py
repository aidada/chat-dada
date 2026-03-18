"""Compatibility shim — real module at capabilities/planner.py"""
import capabilities.planner as _real  # noqa: F401
from capabilities.planner import *  # noqa: F401,F403


def __getattr__(name):  # noqa: N807
    return getattr(_real, name)
