"""Compatibility shim — real module at runtime/task_dispatcher.py"""
import runtime.task_dispatcher as _real  # noqa: F401
from runtime.task_dispatcher import *  # noqa: F401,F403


def __getattr__(name):  # noqa: N807
    return getattr(_real, name)
