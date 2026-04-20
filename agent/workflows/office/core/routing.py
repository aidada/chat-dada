from __future__ import annotations

from typing import Any


def route_after_preflight(state: dict[str, Any]) -> str:
    if state.get("terminal_status") or str(state.get("current_stage", "") or "") == "finalize":
        return "finalize"
    return "resolve_reference_inputs"


def route_after_build(state: dict[str, Any]) -> str:
    if state.get("terminal_status"):
        return "finalize"
    if str(state.get("current_stage", "") or "") == "qa_fix":
        return "qa_fix"
    return "build"


def route_after_qa_fix(state: dict[str, Any]) -> str:
    if str(state.get("current_stage", "") or "") == "build":
        return "build"
    return "finalize"
