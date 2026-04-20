from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field


OfficeFormat: TypeAlias = Literal["pptx", "docx", "xlsx"]
OfficeOperation: TypeAlias = Literal["create", "edit", "inspect", "transform"]
GoalConfidence: TypeAlias = Literal["high", "medium", "low"]


class QualityProfile(BaseModel):
    animations: bool = False
    visuals: bool = False
    notes: bool = False


class GoalNormalizationRequest(BaseModel):
    raw_user_message: str
    orchestrator_summary: str | None = None
    file_hint: str | None = None
    source_files: list[str] = Field(default_factory=list)
    reference_files: list[str] = Field(default_factory=list)
    explicit_format: OfficeFormat | None = None
    explicit_operation: OfficeOperation | None = None
    clarification_history: list[dict[str, Any]] = Field(default_factory=list)


class GoalProfile(BaseModel):
    format: OfficeFormat | None = None
    operation: OfficeOperation | None = None
    requested_slide_count: int | None = Field(default=None, ge=1, le=30)
    output_filename: str | None = None
    source_files: list[str] = Field(default_factory=list)
    reference_files: list[str] = Field(default_factory=list)
    quality_profile: QualityProfile = Field(default_factory=QualityProfile)
    confidence: GoalConfidence = "low"
    missing_fields: list[str] = Field(default_factory=list)


class NormalizeOk(BaseModel):
    kind: Literal["ok"] = "ok"
    profile: GoalProfile


class NeedClarification(BaseModel):
    kind: Literal["need_clarification"] = "need_clarification"
    questions: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class RejectNormalization(BaseModel):
    kind: Literal["reject"] = "reject"
    reason: str


NormalizeResult: TypeAlias = NormalizeOk | NeedClarification | RejectNormalization


__all__ = [
    "GoalConfidence",
    "GoalNormalizationRequest",
    "GoalProfile",
    "NeedClarification",
    "NormalizeOk",
    "NormalizeResult",
    "OfficeFormat",
    "OfficeOperation",
    "QualityProfile",
    "RejectNormalization",
]
