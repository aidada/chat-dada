"""Compatibility shim — real module at storage/user_store.py"""
import storage.user_store as _real  # noqa: F401
from storage.user_store import *  # noqa: F401,F403


def __getattr__(name):  # noqa: N807
    return getattr(_real, name)
