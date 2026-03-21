"""Citation standardization, deduplication, and numbering.

Provides a unified citation interface for all domain agents. Citations are
numbered sequentially within a task, deduplicated by URL, and can be rendered
as footnotes or a references section.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Citation:
    """A single citation entry."""

    citation_id: int
    url: str
    title: str = ""
    snippet: str = ""
    accessed_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class CitationMap:
    """Manages citations for a single task, ensuring deduplication and sequential numbering."""

    def __init__(self) -> None:
        self._by_url: dict[str, Citation] = {}
        self._ordered: list[Citation] = []
        self._next_id: int = 1

    def add(self, url: str, *, title: str = "", snippet: str = "", **metadata: Any) -> Citation:
        """Add a citation. If the URL already exists, return the existing entry."""
        normalized = url.strip().rstrip("/")
        if normalized in self._by_url:
            return self._by_url[normalized]

        citation = Citation(
            citation_id=self._next_id,
            url=normalized,
            title=title,
            snippet=snippet,
            metadata=metadata,
        )
        self._by_url[normalized] = citation
        self._ordered.append(citation)
        self._next_id += 1
        return citation

    def get(self, citation_id: int) -> Citation | None:
        for c in self._ordered:
            if c.citation_id == citation_id:
                return c
        return None

    def get_by_url(self, url: str) -> Citation | None:
        return self._by_url.get(url.strip().rstrip("/"))

    def all(self) -> list[Citation]:
        return list(self._ordered)

    def render_footnotes(self) -> str:
        """Render citations as numbered footnotes."""
        lines = []
        for c in self._ordered:
            label = c.title or c.url
            lines.append(f"[{c.citation_id}] {label} — {c.url}")
        return "\n".join(lines)

    def render_markdown_references(self) -> str:
        """Render citations as a markdown references section."""
        lines = ["## References", ""]
        for c in self._ordered:
            label = c.title or c.url
            lines.append(f"{c.citation_id}. [{label}]({c.url})")
        return "\n".join(lines)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "citation_id": c.citation_id,
                "url": c.url,
                "title": c.title,
                "snippet": c.snippet,
                "metadata": c.metadata,
            }
            for c in self._ordered
        ]
