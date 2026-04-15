from __future__ import annotations

from pydantic import BaseModel, Field


class CreateTaskCommand(BaseModel):
    task: str
    mode: str = "auto"
    thinking_level: str = "medium"
    file_paths: list[str] = Field(default_factory=list)
    conversation_id: str = ""
