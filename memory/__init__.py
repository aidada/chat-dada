"""Compatibility shim — real module at storage/user_store.py"""
from storage.user_store import MemoryRecall, MarkdownMemoryStore, get_memory_store  # noqa: F401

__all__ = ["MemoryRecall", "MarkdownMemoryStore", "get_memory_store"]
