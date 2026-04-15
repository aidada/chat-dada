from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from web.deps import ensure_owner_or_404, get_conversation_service, get_current_user
from domain.conversations.services import ConversationService

router = APIRouter(tags=["conversations"])


class ConversationCreateRequest(BaseModel):
    title: str = "新对话"


class ConversationUpdateRequest(BaseModel):
    title: str | None = None
    pinned: bool | None = None


@router.get("/conversations")
async def list_conversations(
    current_user=Depends(get_current_user),
    conversations: ConversationService = Depends(get_conversation_service),
):
    return await conversations.list_for_user(current_user.id)


@router.post("/conversations")
async def create_conversation(
    payload: ConversationCreateRequest,
    current_user=Depends(get_current_user),
    conversations: ConversationService = Depends(get_conversation_service),
):
    conv_id = f"conv_{uuid.uuid4().hex[:12]}"
    conv = await conversations.create(
        conversation_id=conv_id,
        user_id=current_user.id,
        title=payload.title,
    )
    return conv


@router.patch("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: str,
    payload: ConversationUpdateRequest,
    current_user=Depends(get_current_user),
    conversations: ConversationService = Depends(get_conversation_service),
):
    existing = await conversations.get(conversation_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    ensure_owner_or_404(resource_user_id=str(existing.get("user_id", "")), current_user=current_user)
    fields: dict[str, Any] = {}
    if payload.title is not None:
        fields["title"] = payload.title
    if payload.pinned is not None:
        fields["pinned"] = payload.pinned
    if not fields:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    result = await conversations.update(conversation_id, **fields)
    if result is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return result


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    current_user=Depends(get_current_user),
    conversations: ConversationService = Depends(get_conversation_service),
):
    existing = await conversations.get(conversation_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    ensure_owner_or_404(resource_user_id=str(existing.get("user_id", "")), current_user=current_user)
    deleted = await conversations.delete(conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="对话不存在")
    return Response(status_code=204)


@router.get("/conversations/{conversation_id}/entries")
async def get_conversation_entries(
    conversation_id: str,
    current_user=Depends(get_current_user),
    conversations: ConversationService = Depends(get_conversation_service),
):
    existing = await conversations.get(conversation_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    ensure_owner_or_404(resource_user_id=str(existing.get("user_id", "")), current_user=current_user)
    return await conversations.get_entries(conversation_id)
