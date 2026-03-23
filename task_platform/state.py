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
    stream_schema_version: str
    stream_part_type: str
    event_type: str
    content: str
    name: str
    url: str
    context: str
    placeholder: str
    thread_id: str
    graph_node: str
    graph_path: list[str]
    domain: str
    interrupt_type: str
    node_name: str
    status: str
    phase: str
    task_name: str
    langgraph_task_id: str
    nested_graph: str
    strategy: str
    subtask_id: str
    source: str
    message_metadata: dict[str, Any]
    update_metadata: dict[str, Any]
    update: Any
    input: Any
    result: Any
    error: str
    interrupts: list[Any]
    triggers: list[str]
    next_nodes: list[str]
    checkpoint_metadata: dict[str, Any]
    checkpoint_tasks: list[Any]
    subtasks: list[dict[str, Any]]
    confidence: float
    reasoning: str
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
