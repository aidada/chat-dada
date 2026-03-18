"""Compatibility shim — real module at core/content_utils.py"""
import core.content_utils as _real  # noqa: F401
from core.content_utils import *  # noqa: F401,F403


def __getattr__(name):  # noqa: N807
    return getattr(_real, name)
