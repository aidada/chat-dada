"""Compatibility shim — real module at core/logger.py"""
import core.logger as _real  # noqa: F401
from core.logger import *  # noqa: F401,F403


def __getattr__(name):  # noqa: N807
    return getattr(_real, name)
