"""
Slide DSL — structured JSON schema for PPT content.
LLM agents output this schema. PPT Engine consumes it.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class ChartData(BaseModel):
    type: str = Field(description="Chart type: bar, line, pie, radar")
    data: dict = Field(description='{"labels": [...], "values": [...]} or {"labels": [...], "series": [{"name": "...", "values": [...]}]}')
    unit: Optional[str] = None


class Slide(BaseModel):
    layout: str = Field(description="One of: title_slide, section_header, content_only, content_with_chart, content_with_image, two_column, comparison, summary")
    title: str = ""
    subtitle: Optional[str] = None
    body: Optional[str] = None
    body_left: Optional[str] = None
    body_right: Optional[str] = None
    chart: Optional[ChartData] = None
    image_prompt: Optional[str] = None
    speaker_notes: Optional[str] = None


class DeckMeta(BaseModel):
    title: str
    author: str = ""
    theme: str = "academic_blue"


class SlideDeck(BaseModel):
    meta: DeckMeta
    slides: list[Slide]
