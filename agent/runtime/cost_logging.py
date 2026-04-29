from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

_log = logging.getLogger("chatdada.runtime.cost")
_SLIDE_REF_RE = re.compile(r"/slide\[(\d+)\]")


def init_cost_ledger(
    *,
    task_id: str,
    domain: str,
    requested_pages: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "task_id": str(task_id or ""),
        "domain": str(domain or ""),
        "requested_pages": _coerce_optional_positive_int(requested_pages),
        "completed_pages": 0,
        "total_cost_usd": 0.0,
        "model_cost_usd": 0.0,
        "tool_cost_usd": 0.0,
        "stage_records": [],
        "call_records": [],
        "created_at_ms": _now_ms(),
        "metadata": dict(metadata or {}),
    }


def append_stage_record(
    ledger: dict[str, Any] | None,
    *,
    stage: str,
    status: str,
    elapsed_ms: int = 0,
    cost_usd: float = 0.0,
    model_calls: int = 0,
    tool_calls: int = 0,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active = dict(ledger or {})
    records = list(active.get("stage_records") or [])
    record = {
        "stage": str(stage or ""),
        "status": str(status or ""),
        "elapsed_ms": int(elapsed_ms or 0),
        "cost_usd": float(cost_usd or 0.0),
        "model_calls": int(model_calls or 0),
        "tool_calls": int(tool_calls or 0),
        "metadata": dict(metadata or {}),
        "recorded_at_ms": _now_ms(),
    }
    records.append(record)
    active["stage_records"] = records
    active["total_cost_usd"] = round(float(active.get("total_cost_usd", 0.0) or 0.0) + record["cost_usd"], 6)
    active["model_cost_usd"] = round(float(active.get("model_cost_usd", 0.0) or 0.0), 6)
    active["tool_cost_usd"] = round(float(active.get("tool_cost_usd", 0.0) or 0.0), 6)
    return active


def append_call_record(
    ledger: dict[str, Any] | None,
    *,
    stage: str,
    call_type: str,
    name: str,
    estimated_cost_usd: float = 0.0,
    execution_time_ms: int = 0,
    result_kind: str = "",
    command: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active = dict(ledger or {})
    records = list(active.get("call_records") or [])
    record = {
        "stage": str(stage or ""),
        "call_type": str(call_type or ""),
        "name": str(name or ""),
        "estimated_cost_usd": float(estimated_cost_usd or 0.0),
        "execution_time_ms": int(execution_time_ms or 0),
        "result_kind": str(result_kind or ""),
        "command": str(command or ""),
        "metadata": dict(metadata or {}),
        "recorded_at_ms": _now_ms(),
    }
    records.append(record)
    active["call_records"] = records
    active["total_cost_usd"] = round(float(active.get("total_cost_usd", 0.0) or 0.0) + record["estimated_cost_usd"], 6)
    if record["call_type"] == "model":
        active["model_cost_usd"] = round(float(active.get("model_cost_usd", 0.0) or 0.0) + record["estimated_cost_usd"], 6)
    elif record["call_type"] == "tool":
        active["tool_cost_usd"] = round(float(active.get("tool_cost_usd", 0.0) or 0.0) + record["estimated_cost_usd"], 6)
    return active


def summarize_cost_ledger(ledger: dict[str, Any] | None) -> dict[str, Any]:
    active = dict(ledger or {})
    stage_records = list(active.get("stage_records") or [])
    call_records = list(active.get("call_records") or [])
    summary = {
        "task_id": active.get("task_id", ""),
        "domain": active.get("domain", ""),
        "requested_pages": _coerce_optional_positive_int(active.get("requested_pages")),
        "completed_pages": int(active.get("completed_pages", 0) or 0),
        "total_cost_usd": round(float(active.get("total_cost_usd", 0.0) or 0.0), 6),
        "model_cost_usd": round(float(active.get("model_cost_usd", 0.0) or 0.0), 6),
        "tool_cost_usd": round(float(active.get("tool_cost_usd", 0.0) or 0.0), 6),
        "stage_records": stage_records,
        "call_records": call_records,
    }
    if active.get("quality_report_summary") is not None:
        summary["quality_report_summary"] = _copy_summary_dict(active.get("quality_report_summary"))
    if active.get("partial_progress"):
        summary["partial_progress"] = dict(active.get("partial_progress") or {})
    return summary


def log_cost_record(record_type: str, payload: dict[str, Any]) -> None:
    try:
        rendered = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        rendered = str(payload)
    _log.info("cost.%s %s", str(record_type or "unknown"), rendered)


def merge_llm_usage_into_ledger(
    ledger: dict[str, Any] | None,
    *,
    llm_usage: list[dict[str, Any]],
    estimate_cost: Any,
) -> dict[str, Any]:
    active = dict(ledger or {})
    for item in llm_usage:
        model = str(item.get("model", "") or "")
        role = str(item.get("role", "") or "llm")
        input_tokens = int(item.get("input_tokens", 0) or 0)
        output_tokens = int(item.get("output_tokens", 0) or 0)
        total_tokens = int(item.get("total_tokens", 0) or 0)
        cost = float(
            estimate_cost(
                model=model,
                total_tokens=total_tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            or 0.0
        )
        active = append_call_record(
            active,
            stage=f"llm:{role}",
            call_type="model",
            name=model or role,
            estimated_cost_usd=cost,
            execution_time_ms=0,
            result_kind="completed",
            metadata={
                "role": role,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "calls": int(item.get("calls", 0) or 0),
            },
        )
    return active


def merge_tool_events_into_ledger(
    ledger: dict[str, Any] | None,
    *,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    active = dict(ledger or {})
    for event in events:
        event_type = str(event.get("type", "") or "")
        if event_type not in {"tool.completed", "tool.failed"}:
            continue
        payload = dict(event.get("payload") or {})
        parsed = parse_tool_event_payload(payload)
        active = append_call_record(
            active,
            stage=str(payload.get("stage", "") or "unknown"),
            call_type="tool",
            name=str(payload.get("name", "") or "tool"),
            estimated_cost_usd=0.0,
            execution_time_ms=int(payload.get("execution_time_ms", 0) or 0),
            result_kind=parsed.get("kind", "") or ("completed" if event_type == "tool.completed" else "failed"),
            command=str(parsed.get("command", "") or ""),
            metadata={
                "toolCallId": str(payload.get("toolCallId", "") or ""),
                "message": str(parsed.get("message", "") or payload.get("error", "") or ""),
            },
        )
    return active


def parse_tool_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    output = payload.get("output")
    if not isinstance(output, str) or not output.strip():
        return {
            "kind": "failed" if payload.get("error") else "",
            "command": "",
            "message": str(payload.get("error", "") or ""),
        }
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            return {
                "kind": str(parsed.get("kind", "") or ""),
                "command": str(parsed.get("command", "") or ""),
                "message": str(parsed.get("message", "") or ""),
            }
    except Exception:
        pass
    return {
        "kind": "",
        "command": "",
        "message": output[:200],
    }


def infer_completed_pages_from_events(events: list[dict[str, Any]]) -> int:
    max_slide = 0
    for event in events:
        payload = dict(event.get("payload") or {})
        parsed = parse_tool_event_payload(payload)
        haystacks = [
            str(parsed.get("command", "") or ""),
            str(parsed.get("message", "") or ""),
            str(payload.get("output", "") or ""),
        ]
        for haystack in haystacks:
            for match in _SLIDE_REF_RE.findall(haystack):
                try:
                    max_slide = max(max_slide, int(match))
                except (TypeError, ValueError):
                    continue
    return max_slide


def build_failure_diagnostics(events: list[dict[str, Any]]) -> dict[str, Any]:
    completed_pages = infer_completed_pages_from_events(events)
    tool_events = [event for event in events if str(event.get("type", "") or "").startswith("tool.")]
    last_success = next((event for event in reversed(tool_events) if event.get("type") == "tool.completed"), None)
    payload = dict(last_success.get("payload") or {}) if isinstance(last_success, dict) else {}
    parsed = parse_tool_event_payload(payload) if payload else {}
    return {
        "completed_pages": completed_pages,
        "current_stage": str(payload.get("stage", "") or "unknown"),
        "last_successful_tool": {
            "name": str(payload.get("name", "") or ""),
            "command": str(parsed.get("command", "") or ""),
            "message": str(parsed.get("message", "") or ""),
        },
    }


def attach_quality_summary(
    ledger: dict[str, Any] | None,
    *,
    quality_report_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    active = dict(ledger or {})
    if quality_report_summary is not None:
        active["quality_report_summary"] = _copy_summary_dict(quality_report_summary)
    return active


def attach_partial_progress(
    ledger: dict[str, Any] | None,
    *,
    partial_progress: dict[str, Any] | None,
) -> dict[str, Any]:
    active = dict(ledger or {})
    if partial_progress:
        active["partial_progress"] = dict(partial_progress)
        completed_pages = int(partial_progress.get("completed_pages", 0) or 0)
        if completed_pages:
            active["completed_pages"] = max(int(active.get("completed_pages", 0) or 0), completed_pages)
    return active


def update_completed_pages(
    ledger: dict[str, Any] | None,
    *,
    completed_pages: int,
) -> dict[str, Any]:
    active = dict(ledger or {})
    if completed_pages:
        active["completed_pages"] = max(int(active.get("completed_pages", 0) or 0), int(completed_pages))
    return active


def _now_ms() -> int:
    return int(time.time() * 1000)


def _copy_summary_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return dict(payload)


def _coerce_optional_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
