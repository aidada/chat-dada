"""Utilities for de-duplicating semantically identical retrieval calls."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def build_query_fingerprint(
    *,
    tool_name: str,
    query: str,
    mode: str = "",
    category: str = "",
    summary_query: str = "",
) -> str:
    payload = {
        "tool": _normalize_text(tool_name),
        "query": _normalize_text(query),
        "mode": _normalize_text(mode),
        "category": _normalize_text(category),
        "summary_query": _normalize_text(summary_query or query),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass
class RetrievalCacheEntry:
    fingerprint: str
    tool_name: str
    query: str
    mode: str = ""
    category: str = ""
    summary_query: str = ""
    result: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "tool_name": self.tool_name,
            "query": self.query,
            "mode": self.mode,
            "category": self.category,
            "summary_query": self.summary_query,
            "result": self.result,
            "evidence": list(self.evidence),
            "metadata": dict(self.metadata),
        }


class RetrievalCache:
    """In-memory lookup keyed by normalized retrieval fingerprints."""

    def __init__(self, existing: dict[str, dict[str, Any]] | None = None) -> None:
        self._entries: dict[str, dict[str, Any]] = dict(existing or {})

    def has(self, fingerprint: str) -> bool:
        return fingerprint in self._entries

    def get(self, fingerprint: str) -> dict[str, Any] | None:
        return self._entries.get(fingerprint)

    def put(self, entry: RetrievalCacheEntry) -> None:
        self._entries[entry.fingerprint] = entry.to_dict()

    def export(self) -> dict[str, dict[str, Any]]:
        return dict(self._entries)
