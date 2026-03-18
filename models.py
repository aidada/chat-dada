"""Compatibility shim — real module at core/models.py"""
import core.models as _real  # noqa: F401
from core.models import *  # noqa: F401,F403


def __getattr__(name):  # noqa: N807
    return getattr(_real, name)
