"""Compatibility shim — real module at runtime/task_runtime.py"""
import runtime.task_runtime as _real  # noqa: F401
from runtime.task_runtime import *  # noqa: F401,F403


def __getattr__(name):  # noqa: N807
    return getattr(_real, name)
