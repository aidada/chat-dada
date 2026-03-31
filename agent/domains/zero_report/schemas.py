from __future__ import annotations

from pydantic import BaseModel, Field


class IncidentFactSet(BaseModel):
    title: str = ""
    summary: str = ""
    impacted_scope: str = ""


class TimelineEvent(BaseModel):
    timestamp: str
    detail: str


class Timeline(BaseModel):
    events: list[TimelineEvent] = Field(default_factory=list)


class RootCauseNode(BaseModel):
    label: str
    children: list["RootCauseNode"] = Field(default_factory=list)


class RootCauseTree(BaseModel):
    root: RootCauseNode


class ActionItem(BaseModel):
    owner: str
    due_date: str
    action: str


class ActionMatrix(BaseModel):
    items: list[ActionItem] = Field(default_factory=list)


class ZeroReportDraft(BaseModel):
    title: str = ""
    executive_summary: str = ""
    remediation_plan: str = ""


RootCauseNode.model_rebuild()

