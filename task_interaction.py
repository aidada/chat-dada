"""Compatibility shim — real module at runtime/task_interaction.py"""
import runtime.task_interaction as _real  # noqa: F401
from runtime.task_interaction import *  # noqa: F401,F403


def __getattr__(name):  # noqa: N807
    return getattr(_real, name)
