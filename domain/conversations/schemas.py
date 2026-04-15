from __future__ import annotations

from pydantic import BaseModel


class ConversationUpdateCommand(BaseModel):
    title: str | None = None
    pinned: bool | None = None
