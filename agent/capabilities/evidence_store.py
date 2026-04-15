"""Unified evidence abstraction for research, patent, and zero-report domains.

All domain agents produce evidence (URLs, files, quotes, data) during execution.
This module defines a common schema and storage interface so evidence can be
traced back to its source regardless of which domain produced it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


EvidenceType = Literal["url", "file", "quote", "table", "screenshot"]


@dataclass
class EvidenceItem:
    """A single piece of evidence collected during task execution."""

    evidence_id: str
    evidence_type: EvidenceType
    source: str
    summary: str = ""
    content: str = ""
    collected_at: str = ""
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.collected_at:
            self.collected_at = datetime.now(UTC).isoformat()


@dataclass
class EvidenceCollection:
    """An ordered collection of evidence items for a task."""

    task_id: str
    items: list[EvidenceItem] = field(default_factory=list)

    def add(self, item: EvidenceItem) -> None:
        self.items.append(item)

    def by_type(self, evidence_type: EvidenceType) -> list[EvidenceItem]:
        return [item for item in self.items if item.evidence_type == evidence_type]

    def sources(self) -> list[str]:
        return list(dict.fromkeys(item.source for item in self.items if item.source))


class EvidenceStore(ABC):
    """Abstract interface for persisting and querying evidence."""

    @abstractmethod
    async def save(self, task_id: str, item: EvidenceItem) -> None:
        """Persist a single evidence item."""

    @abstractmethod
    async def get_collection(self, task_id: str) -> EvidenceCollection:
        """Retrieve all evidence for a task."""

    @abstractmethod
    async def query_by_source(self, source: str) -> list[EvidenceItem]:
        """Find all evidence originating from a given source URL/path."""
