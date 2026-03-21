from __future__ import annotations

import operator
from typing import Annotated, Any, Literal

from typing_extensions import NotRequired, TypedDict


class RouteDecisionPayload(TypedDict):
    route_name: str
    reason: str
    confidence: float
    execution_path: Literal["general_chat", "research", "patent", "zero_report", "ppt", "needs_clarification"]


class UIEventPayload(TypedDict, total=False):
    event_type: str
    content: str
    name: str
    url: str
    context: str
    placeholder: str
    thread_id: str
    graph_node: str
    domain: str
    interrupt_type: str
    artifact_refs: list[dict[str, Any]]
    checkpoint_id: str
    trace_metadata: dict[str, Any]


class RootState(TypedDict, total=False):
    task_id: str
    thread_id: str
    user_id: str
    mode: str
    thinking_level: str
    task_text: str
    execution_task: str
    file_paths: list[str]
    attachments: list[dict[str, Any]]
    conversation_id: str
    conversation_context: str
    request_payload: dict[str, Any]
    initial_route_payload: dict[str, Any]
    route_decision: RouteDecisionPayload
    route_name: str
    route_reason: str
    route_confidence: float
    domain: str
    needs_clarification: bool
    clarification_prompt: NotRequired[dict[str, Any]]
    pending_question: NotRequired[dict[str, Any]]
    final_result: str
    error: str
    artifact_refs: list[dict[str, Any]]
    interrupt_state: dict[str, Any] | None
    review: dict[str, Any]
    research_strategy: str
    budget: dict[str, Any]
    trace_metadata: dict[str, Any]
    latest_checkpoint_id: str
    ui_events: Annotated[list[UIEventPayload], operator.add]
