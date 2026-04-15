"""PPT 领域工具集合。"""
from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any

from langchain_core.tools import tool


_ALLOWED_OFFICECLI_ROOT_TOKENS = {
    "create",
    "open",
    "add",
    "set",
    "get",
    "query",
    "validate",
    "remove",
    "view",
    "batch",
    "help",
    "pptx",
    "docx",
    "xlsx",
}
_DISALLOWED_SHELL_PATTERNS = ("&&", "||", ";", "|", ">", "<", "$(", "`", "\n", "\r")


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _looks_like_help_output(output_lower: str) -> bool:
    help_markers = (
        "usage:",
        "available commands",
        "commands:",
        "examples:",
        "options:",
    )
    return any(marker in output_lower for marker in help_markers)


def _reject_invalid_officecli_command(command: str) -> dict[str, Any] | None:
    stripped = str(command or "").strip()
    if not stripped:
        return {
            "success": False,
            "command": command,
            "exit_status": 1,
            "kind": "fatal_error",
            "message": "Empty OfficeCLI command is not allowed.",
            "raw_stdout": "",
            "raw_stderr": "",
        }

    if any(pattern in stripped for pattern in _DISALLOWED_SHELL_PATTERNS):
        return {
            "success": False,
            "command": command,
            "exit_status": 1,
            "kind": "fatal_error",
            "message": "Rejected unsafe shell syntax in officecli command.",
            "raw_stdout": "",
            "raw_stderr": "",
        }

    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        return {
            "success": False,
            "command": command,
            "exit_status": 1,
            "kind": "fatal_error",
            "message": f"Rejected malformed officecli command: {exc}",
            "raw_stdout": "",
            "raw_stderr": "",
        }

    root = tokens[0].lower() if tokens else ""
    if root not in _ALLOWED_OFFICECLI_ROOT_TOKENS:
        return {
            "success": False,
            "command": command,
            "exit_status": 1,
            "kind": "fatal_error",
            "message": f"Rejected non-OfficeCLI command root: {root or '<empty>'}",
            "raw_stdout": "",
            "raw_stderr": "",
        }

    return None


def _classify_officecli_result(
    *,
    command: str,
    success: bool,
    exit_status: int | None,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    combined = "\n".join(part for part in (stdout, stderr) if part).strip()
    combined_lower = combined.lower()
    command_lower = command.strip().lower()

    retryable_markers = (
        "timeout",
        "temporarily",
        "try again",
        "busy",
        "connection reset",
        "network error",
        "rate limit",
    )
    fatal_markers = (
        "command not found",
        "unknown command",
        "invalid command",
        "no such file",
        "not found",
        "permission denied",
        "missing required",
        "cannot open",
        "failed to create",
        "not a valid",
    )
    validation_failure_markers = (
        "validation failed",
        "issues found",
        "invalid",
        "error",
    )

    if not combined:
        if success:
            kind = "success"
            message = "Command completed successfully with no output."
        else:
            kind = "fatal_error" if exit_status not in (None, 0) else "retryable_error"
            message = "Command failed with no diagnostic output."
    elif _looks_like_help_output(combined_lower):
        kind = "help"
        message = _first_meaningful_line(combined) or "Help output returned."
    elif command_lower.startswith("validate "):
        if success and not any(marker in combined_lower for marker in validation_failure_markers):
            kind = "validation_passed"
            message = _first_meaningful_line(combined) or "Validation passed."
        else:
            kind = "validation_failed"
            message = _first_meaningful_line(combined) or "Validation failed."
    elif success:
        kind = "success"
        message = _first_meaningful_line(combined) or "Command completed successfully."
    elif any(marker in combined_lower for marker in retryable_markers):
        kind = "retryable_error"
        message = _first_meaningful_line(combined) or "Retryable error."
    elif any(marker in combined_lower for marker in fatal_markers):
        kind = "fatal_error"
        message = _first_meaningful_line(combined) or "Fatal error."
    else:
        kind = "fatal_error"
        message = _first_meaningful_line(combined) or "Command failed."

    return {
        "success": success if kind not in {"fatal_error", "retryable_error", "validation_failed"} else False,
        "command": command,
        "exit_status": exit_status,
        "kind": kind,
        "message": message,
        "raw_stdout": stdout,
        "raw_stderr": stderr,
    }


def _serialize_officecli_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _aggregate_batch_kind(results: list[dict[str, Any]]) -> tuple[bool, str]:
    priority = (
        "fatal_error",
        "retryable_error",
        "validation_failed",
        "help",
        "success",
        "validation_passed",
    )
    kinds = {result.get("kind", "success") for result in results}
    for kind in priority:
        if kind in kinds:
            overall_kind = kind
            break
    else:
        overall_kind = "success"

    overall_success = all(bool(result.get("success")) for result in results)
    return overall_success, overall_kind


async def _run_officecli_locally(command: str) -> dict[str, Any]:
    result = subprocess.run(
        ["officecli"] + shlex.split(command),
        capture_output=True,
        text=True,
    )
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "exit_status": result.returncode,
    }


async def _execute_officecli_via_gateway(
    *,
    command: str,
) -> dict[str, Any] | None:
    try:
        from langgraph.config import get_config
        from agent.hands import ToolCall, ToolContext
    except Exception:
        return None

    try:
        configurable = get_config().get("configurable", {}) or {}
    except Exception:
        return None

    tool_gateway = configurable.get("tool_gateway")
    user_id = str(configurable.get("request_user_id", "") or "")
    task_id = str(configurable.get("thread_id", "") or "")
    if tool_gateway is None or not user_id or not task_id:
        return None

    try:
        tool_gateway.set_route("officecli", "desktop")
    except Exception:
        pass

    result = await tool_gateway.execute(
        ToolCall(
            tool_name="officecli",
            params={"command": command},
            task_id=task_id,
        ),
        ToolContext(
            user_id=user_id,
            task_id=task_id,
            trace_id=task_id,
        ),
    )
    return {
        "success": result.success,
        "stdout": result.output or "",
        "stderr": result.error or "",
        "exit_status": 0 if result.success else 1,
    }


@tool
async def officecli_run(command: str) -> str:
    """执行 officecli CLI 命令（单条），返回结构化 JSON 字符串。"""
    rejected = _reject_invalid_officecli_command(command)
    if rejected is not None:
        return _serialize_officecli_result(rejected)
    raw_result = await _execute_officecli_via_gateway(command=command)
    if raw_result is None:
        raw_result = await _run_officecli_locally(command)
    return _serialize_officecli_result(
        _classify_officecli_result(command=command, **raw_result)
    )


@tool
async def officecli_batch(commands: list[str]) -> str:
    """批量执行 officecli CLI 命令，返回结构化 JSON 字符串。"""
    rejected_results = []
    for cmd in commands:
        rejected = _reject_invalid_officecli_command(cmd)
        if rejected is not None:
            rejected_results.append(rejected)

    if rejected_results:
        return _serialize_officecli_result(
            {
                "success": False,
                "command": "officecli_batch",
                "exit_status": 1,
                "kind": "fatal_error",
                "message": "Batch rejected due to invalid OfficeCLI command input.",
                "results": rejected_results,
            }
        )

    results = []
    for cmd in commands:
        raw_result = await _execute_officecli_via_gateway(command=cmd)
        if raw_result is None:
            raw_result = await _run_officecli_locally(cmd)
        results.append(_classify_officecli_result(command=cmd, **raw_result))

    overall_success, overall_kind = _aggregate_batch_kind(results)
    overall_message = (
        "All commands completed successfully."
        if overall_success
        else f"Batch finished with {overall_kind}."
    )
    return _serialize_officecli_result(
        {
            "success": overall_success,
            "command": "officecli_batch",
            "exit_status": 0 if overall_success else 1,
            "kind": overall_kind,
            "message": overall_message,
            "results": results,
        }
    )


def get_ppt_tools():
    """Return tools available to PPT domain."""
    return [officecli_run, officecli_batch]
