from __future__ import annotations

from pydantic import BaseModel, Field


class TechnicalDisclosure(BaseModel):
    title: str = ""
    summary: str = ""
    key_terms: list[str] = Field(default_factory=list)
    problem_statement: str = ""
    proposed_solution: str = ""


class PriorArtItem(BaseModel):
    title: str = ""
    source: str = ""
    summary: str = ""
    relation_to_claims: list[str] = Field(default_factory=list)


class ClaimNode(BaseModel):
    claim_id: str
    text: str
    depends_on: list[str] = Field(default_factory=list)


class ClaimTree(BaseModel):
    claims: list[ClaimNode] = Field(default_factory=list)


class PriorArtMatrixRow(BaseModel):
    claim_id: str
    prior_art_title: str
    coverage_note: str


class PriorArtMatrix(BaseModel):
    rows: list[PriorArtMatrixRow] = Field(default_factory=list)


class SpecDraft(BaseModel):
    title: str = ""
    background: str = ""
    summary: str = ""
    embodiments: list[str] = Field(default_factory=list)


class PatentRiskNote(BaseModel):
    severity: str = "warning"
    message: str = ""

