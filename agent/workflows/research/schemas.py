from __future__ import annotations

from pydantic import BaseModel, Field


class ResearchDomainResult(BaseModel):
    status: str
    result: str
    artifact_refs: list[dict[str, str | int | float | bool]] = Field(default_factory=list)
    review: dict = Field(default_factory=dict)
    budget: dict = Field(default_factory=dict)
    strategy: str = ""


class ResearchBrief(BaseModel):
    raw_query: str
    clarified_goal: str
    discipline: str = ""
    deliverable_type: str = "literature_review"
    research_mode: str = "review"
    time_scope: str = "recent + seminal"
    literature_languages: list[str] = Field(default_factory=lambda: ["en"])
    citation_style: str = "APA"
    output_language: str = "zh-CN"
    user_constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    preferred_emphasis: list[str] = Field(default_factory=list)


class ResearchModulePlan(BaseModel):
    module_id: str
    title: str
    module_type: str
    owner_role: str
    objective: str
    depends_on: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    required_output_fields: list[str] = Field(default_factory=list)
    evaluation_dimensions: list[str] = Field(default_factory=list)
    revision_policy: str = "replace_module"
    checkpoint_after: str | None = None


class ResearchModuleDraft(BaseModel):
    module_id: str
    version: int = 1
    status: str = "completed"
    content: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    citation_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_gaps: list[str] = Field(default_factory=list)
    last_worker_role: str = "argument_worker"
    last_review_score: float = 0.0
    locked: bool = False


class ResearchEvidence(BaseModel):
    evidence_id: str
    title: str = ""
    url: str = ""
    source_type: str = "web"
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    snippet: str = ""
    claim_supported: str = ""
    relevance_score: float = 0.0
    recency_score: float = 0.0
    traceable: bool = False


class ReviewDimensionModel(BaseModel):
    name: str
    score: float
    passed: bool
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    affected_modules: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class RevisionTargetModel(BaseModel):
    module_id: str
    reason: str
    priority: str = "medium"
    actions: list[str] = Field(default_factory=list)
    preserve_constraints: list[str] = Field(default_factory=list)
    requires_new_evidence: bool = False
    metadata: dict = Field(default_factory=dict)


class ResearchEvaluation(BaseModel):
    passed: bool = False
    needs_replan: bool = False
    summary: str = ""
    dimensions: list[ReviewDimensionModel] = Field(default_factory=list)
    revision_targets: list[RevisionTargetModel] = Field(default_factory=list)
    lock_modules: list[str] = Field(default_factory=list)
    user_feedback_required: bool = False
    issues: list[dict] = Field(default_factory=list)


class WorkerResult(BaseModel):
    module_id: str
    topic: str
    status: str
    findings: str = ""
    evidence: list[dict] = Field(default_factory=list)
    blocker_reason: str = ""
    search_stats: dict = Field(default_factory=dict)
    error: str = ""
