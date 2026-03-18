"""Compatibility shim — real module at capabilities/progress_tracker.py"""
import capabilities.progress_tracker as _real  # noqa: F401
from capabilities.progress_tracker import *  # noqa: F401,F403


def __getattr__(name):  # noqa: N807
    return getattr(_real, name)
