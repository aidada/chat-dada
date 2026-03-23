from __future__ import annotations

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    title: str = ""
    snippet: str = ""
    url: str = ""
    source_type: str = "web"


class CitationMap(BaseModel):
    citations: list[EvidenceItem] = Field(default_factory=list)


class WorkerResult(BaseModel):
    subtask_id: str
    topic: str
    status: str
    findings: str = ""
    error: str = ""

