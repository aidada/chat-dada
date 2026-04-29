from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from threading import Lock
from typing import Any

from agent.hands import ToolCall, ToolContext
from agent.tools.officecli_skill_loader import build_officecli_skill_bundle

ALLOWED_DIR = Path(os.getenv("OFFICECLI_ALLOWED_DIR", "outputs")).resolve()
OFFICE_CONSTRAINTS_KEY = "office_constraints"

SUPPORTED_FORMATS = {"pptx", "docx", "xlsx"}
SUPPORTED_VERBS = {
    "create",
    "open",
    "close",
    "view",
    "get",
    "query",
    "set",
    "add",
    "remove",
    "validate",
    "help",
    "watch",
    "unwatch",
}
_ALLOWED_OFFICECLI_ROOT_TOKENS = SUPPORTED_VERBS | SUPPORTED_FORMATS | {"batch", "raw"}
_DISALLOWED_SHELL_PATTERNS = ("&&", "||", ";", "|", ">", "<", "$(", "`", "\n", "\r")
_VERBS_WITH_FILE_ARG = {
    "create",
    "open",
    "close",
    "view",
    "get",
    "query",
    "set",
    "add",
    "remove",
    "validate",
    "watch",
    "unwatch",
}
_FOUND_VALIDATION_ERRORS_RE = re.compile(r"found\s+(\d+)\s+validation error(?:\(s\))?", re.IGNORECASE)
_OFFICE_FATAL_BLOCK_LOCK = Lock()
_OFFICE_FATAL_BLOCKS: dict[str, dict[str, dict[str, Any]]] = {}


def _office_gateway_timeout_ms(spec: dict[str, Any] | None = None) -> int:
    default_timeout_ms = 120_000
    candidate: Any = None
    if isinstance(spec, dict):
        candidate = spec.get("timeout_ms")
        if candidate is None:
            options = spec.get("options")
            if isinstance(options, dict):
                candidate = options.get("timeout_ms")
    try:
        timeout_ms = int(candidate) if candidate is not None else default_timeout_ms
    except (TypeError, ValueError):
        timeout_ms = default_timeout_ms
    return max(timeout_ms, 1)


def _office_task_scope() -> str:
    configurable = _get_configurable()
    return str(
        configurable.get("thread_id")
        or configurable.get("task_id")
        or configurable.get("trace_id")
        or "office-global"
    )


def _blocked_repeated_fatal_command(scope: str, command: str, runtime_target: str) -> dict[str, Any] | None:
    with _OFFICE_FATAL_BLOCK_LOCK:
        task_blocks = _OFFICE_FATAL_BLOCKS.get(scope, {})
        record = task_blocks.get(command)
    if not record:
        return None
    return {
        "success": False,
        "command": command,
        "exit_status": 1,
        "kind": "fatal_error",
        "message": f"Repeated fatal OfficeCLI command blocked: {record.get('message', 'previous fatal error')}",
        "raw_stdout": "",
        "raw_stderr": str(record.get("message", "") or "previous fatal error"),
        "runtime_target": runtime_target,
        "artifacts": [],
    }


def _remember_fatal_command(scope: str, command: str, payload: dict[str, Any]) -> None:
    with _OFFICE_FATAL_BLOCK_LOCK:
        task_blocks = _OFFICE_FATAL_BLOCKS.setdefault(scope, {})
        task_blocks[command] = {
            "message": str(payload.get("message", "") or ""),
            "kind": str(payload.get("kind", "") or "fatal_error"),
        }


def _clear_fatal_command(scope: str, command: str) -> None:
    with _OFFICE_FATAL_BLOCK_LOCK:
        task_blocks = _OFFICE_FATAL_BLOCKS.get(scope)
        if not task_blocks:
            return
        task_blocks.pop(command, None)
        if not task_blocks:
            _OFFICE_FATAL_BLOCKS.pop(scope, None)


def _structured_spec_error(
    *,
    command: str,
    runtime_target: str,
    message: str,
) -> dict[str, Any]:
    return {
        "success": False,
        "command": command,
        "exit_status": 1,
        "kind": "fatal_error",
        "message": message,
        "raw_stdout": "",
        "raw_stderr": message,
        "runtime_target": runtime_target,
        "artifacts": [],
    }


def _validate_structured_spec(spec: dict[str, Any], *, runtime_target: str) -> dict[str, Any] | None:
    command = _render_officecli_command(spec)
    verb = str(spec.get("verb") or "").strip().lower()
    if verb == "help":
        format_name = _normalize_optional_string(spec.get("format"))
        options = spec.get("options") if isinstance(spec.get("options"), dict) else {}
        if not format_name and not _normalize_optional_string(options.get("format")):
            return _structured_spec_error(command=command, runtime_target=runtime_target, message='OfficeCLI help requires "format".')
        return None
    if verb == "view" and not _normalize_optional_string(spec.get("mode")):
        return _structured_spec_error(command=command, runtime_target=runtime_target, message='OfficeCLI view requires a non-empty "mode" parameter.')
    if verb in {"get", "remove", "set"} and not _normalize_optional_string(spec.get("path")):
        return _structured_spec_error(command=command, runtime_target=runtime_target, message=f'OfficeCLI {verb} requires a non-empty "path" parameter.')
    if verb == "query" and not _normalize_optional_string(spec.get("selector")):
        return _structured_spec_error(command=command, runtime_target=runtime_target, message='OfficeCLI query requires a non-empty "selector" parameter.')
    if verb == "add":
        if not _normalize_optional_string(spec.get("parent")):
            return _structured_spec_error(command=command, runtime_target=runtime_target, message='OfficeCLI add requires a non-empty "parent" parameter.')
        if not _normalize_optional_string(spec.get("type")):
            return _structured_spec_error(command=command, runtime_target=runtime_target, message='OfficeCLI add requires a non-empty "type" parameter.')
    if verb == "set" and not _normalize_props(spec.get("props")):
        return _structured_spec_error(command=command, runtime_target=runtime_target, message='OfficeCLI set requires non-empty "props".')
    return None


def _detect_validation_error_count(combined: str) -> int | None:
    match = _FOUND_VALIDATION_ERRORS_RE.search(str(combined or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


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
        "description:",
    )
    return any(marker in output_lower for marker in help_markers)


def _get_configurable() -> dict[str, Any]:
    try:
        from langgraph.config import get_config

        configurable = get_config().get("configurable", {}) or {}
    except Exception:
        configurable = {}
    return dict(configurable)


def infer_office_runtime_target(configurable: dict[str, Any] | None = None) -> str:
    config = dict(configurable or _get_configurable())
    constraints = config.get(OFFICE_CONSTRAINTS_KEY)
    if isinstance(constraints, dict):
        runtime_target = str(constraints.get("runtime_target", "") or "").strip().lower()
        if runtime_target in {"desktop", "server"}:
            return runtime_target

    desktop_manager = config.get("desktop_manager")
    user_id = str(config.get("request_user_id", "") or "")
    if desktop_manager is not None and user_id:
        try:
            if desktop_manager.is_connected(user_id):
                return "desktop"
        except Exception:
            pass
    return "server"


def get_office_constraints(configurable: dict[str, Any] | None = None) -> dict[str, Any]:
    config = dict(configurable or _get_configurable())
    raw_constraints = config.get(OFFICE_CONSTRAINTS_KEY)
    constraints = dict(raw_constraints) if isinstance(raw_constraints, dict) else {}

    allowed_output_dir = str(constraints.get("allowed_output_dir", "") or "").strip() or str(ALLOWED_DIR)
    raw_sources = constraints.get("allowed_source_files")
    allowed_source_files = []
    if isinstance(raw_sources, list):
        allowed_source_files = [str(item).strip() for item in raw_sources if str(item).strip()]
    default_create_file = str(constraints.get("default_create_file", "") or "").strip()

    return {
        "runtime_target": infer_office_runtime_target(config),
        "allowed_output_dir": allowed_output_dir,
        "allowed_source_files": allowed_source_files,
        "default_create_file": default_create_file,
    }


def _normalize_gateway_artifacts(artifacts: Any, runtime_target: str) -> list[dict[str, Any]]:
    if not isinstance(artifacts, list):
        return []

    normalized: list[dict[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        entry = {str(key): value for key, value in artifact.items() if value not in (None, "")}
        entry.setdefault("location", runtime_target)
        if entry.get("path") and not entry.get("display_path"):
            entry["display_path"] = str(entry["path"])
        normalized.append(entry)
    return normalized


def _classify_officecli_result(
    *,
    command: str,
    success: bool,
    exit_status: int | None,
    stdout: str,
    stderr: str,
    runtime_target: str = "server",
    artifacts: list[dict[str, Any]] | None = None,
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
    validation_success_markers = (
        "validation passed",
        "no errors found",
        "no error found",
        "no issues found",
        "0 issues found",
    )
    validation_failure_markers = (
        "validation failed",
        "issues found",
        "issue found",
        "errors found",
        "error found",
        "not valid",
    )
    desktop_timeout_markers = (
        "desktop tool call timeout after",
        "permission dialog unavailable",
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
    elif command_lower.startswith("validate ") or command_lower.startswith("officecli validate "):
        has_validation_success_marker = any(marker in combined_lower for marker in validation_success_markers)
        has_validation_failure_marker = any(marker in combined_lower for marker in validation_failure_markers)
        detected_error_count = _detect_validation_error_count(combined)
        if detected_error_count is not None:
            if detected_error_count > 0:
                kind = "validation_failed"
                message = _first_meaningful_line(combined) or f"Validation failed with {detected_error_count} error(s)."
            else:
                kind = "validation_passed"
                message = _first_meaningful_line(combined) or "Validation passed."
        elif has_validation_success_marker:
            kind = "validation_passed"
            message = _first_meaningful_line(combined) or "Validation passed."
        elif has_validation_failure_marker:
            kind = "validation_failed"
            message = _first_meaningful_line(combined) or "Validation failed."
        elif success:
            kind = "validation_passed"
            message = _first_meaningful_line(combined) or "Validation passed."
        else:
            kind = "validation_failed"
            message = _first_meaningful_line(combined) or "Validation failed."
    elif success:
        kind = "success"
        message = _first_meaningful_line(combined) or "Command completed successfully."
    elif runtime_target == "desktop" and any(marker in combined_lower for marker in desktop_timeout_markers):
        kind = "fatal_error"
        message = _first_meaningful_line(combined) or "Desktop tool execution timed out."
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
        "runtime_target": runtime_target,
        "artifacts": list(artifacts or []),
    }


def _normalize_path_text(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/")


def _basename(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("\\", "/").rsplit("/", 1)[-1]


def _ensure_supported_extension(value: str) -> str:
    text = str(value or "").strip()
    suffix = Path(_basename(text)).suffix.lower().lstrip(".")
    if suffix not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported Office file extension: {suffix or '<empty>'}")
    return suffix


def _normalize_server_path(value: str) -> Path:
    text = str(value or "").strip()
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve(strict=False)
    else:
        candidate = candidate.resolve(strict=False)
    return candidate


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _normalize_file_arg(
    value: Any,
    *,
    verb: str | None = None,
    constraints: dict[str, Any] | None = None,
) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    constraints = dict(constraints or get_office_constraints())
    runtime_target = str(constraints.get("runtime_target", "server") or "server")
    bare_name = _basename(text)
    has_directory = any(sep in text for sep in ("/", "\\"))

    _ensure_supported_extension(text)

    if verb == "create":
        if has_directory or text != bare_name:
            raise ValueError('OfficeCLI create only allows bare filenames.')
        return bare_name

    if not has_directory and text == bare_name:
        return bare_name

    if runtime_target == "desktop":
        allowed = {
            _normalize_path_text(item)
            for item in constraints.get("allowed_source_files", [])
            if str(item).strip()
        }
        normalized = _normalize_path_text(text)
        if normalized in allowed:
            return text
        raise ValueError("OfficeCLI desktop file path is not authorized for this task.")

    allowed_output_dir = Path(str(constraints.get("allowed_output_dir", ALLOWED_DIR) or ALLOWED_DIR)).expanduser().resolve(strict=False)
    candidate = _normalize_server_path(text)
    if _is_within(candidate, allowed_output_dir):
        return str(candidate)

    for source in constraints.get("allowed_source_files", []):
        source_text = str(source).strip()
        if not source_text:
            continue
        source_path = _normalize_server_path(source_text)
        if candidate == source_path:
            return str(candidate)

    raise ValueError("OfficeCLI file path is not authorized for this task.")


def _validate_raw_command_file_token(root: str, tokens: list[str], constraints: dict[str, Any]) -> None:
    if root not in _VERBS_WITH_FILE_ARG or len(tokens) < 2:
        return
    _normalize_file_arg(tokens[1], verb=root, constraints=constraints)


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
            "runtime_target": infer_office_runtime_target(),
            "artifacts": [],
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
            "runtime_target": infer_office_runtime_target(),
            "artifacts": [],
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
            "runtime_target": infer_office_runtime_target(),
            "artifacts": [],
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
            "runtime_target": infer_office_runtime_target(),
            "artifacts": [],
        }

    try:
        _validate_raw_command_file_token(root, tokens, get_office_constraints())
    except ValueError as exc:
        return {
            "success": False,
            "command": command,
            "exit_status": 1,
            "kind": "fatal_error",
            "message": str(exc),
            "raw_stdout": "",
            "raw_stderr": "",
            "runtime_target": infer_office_runtime_target(),
            "artifacts": [],
        }

    return None


def _normalize_structured_spec(
    spec: dict[str, Any],
    *,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = dict(spec)
    if "verb" not in normalized:
        legacy_verb = normalized.pop("command", None)
        if legacy_verb is None:
            legacy_verb = normalized.pop("operation", None)
        if legacy_verb is not None:
            normalized["verb"] = legacy_verb
    if "props" not in normalized and "properties" in normalized:
        normalized["props"] = normalized.pop("properties")
    verb = str(normalized.get("verb") or "").strip().lower()
    current_file = _normalize_optional_string(normalized.get("file"))
    if current_file is None and verb in _VERBS_WITH_FILE_ARG:
        active_constraints = dict(constraints or get_office_constraints())
        default_file = _normalize_default_file_for_verb(verb, active_constraints)
        if default_file is not None:
            normalized["file"] = default_file
    return normalized


def _normalize_default_file_for_verb(verb: str, constraints: dict[str, Any]) -> str | None:
    default_create_file = _normalize_optional_string(constraints.get("default_create_file"))
    if default_create_file is not None:
        return default_create_file

    raw_sources = constraints.get("allowed_source_files")
    if isinstance(raw_sources, list):
        candidates = [_normalize_optional_string(item) for item in raw_sources]
        unique_candidates = [item for item in candidates if item]
        if len(unique_candidates) == 1:
            return unique_candidates[0]

    return None


def compile_officecli_argv(spec: dict[str, Any], *, constraints: dict[str, Any] | None = None) -> list[str]:
    spec = _normalize_structured_spec(spec, constraints=constraints)
    verb = str(spec.get("verb") or "").strip().lower()
    if verb not in SUPPORTED_VERBS:
        raise ValueError(f"Unsupported OfficeCLI verb: {verb or '<empty>'}")

    active_constraints = dict(constraints or get_office_constraints())
    file = _normalize_file_arg(spec.get("file"), verb=verb, constraints=active_constraints)
    path = _normalize_optional_string(spec.get("path"))
    parent = _normalize_optional_string(spec.get("parent"))
    content_type = _normalize_optional_string(spec.get("type"))
    mode = _normalize_optional_string(spec.get("mode"))
    selector = _normalize_optional_string(spec.get("selector"))
    fmt = _normalize_optional_string(spec.get("format"))
    props = _normalize_props(spec.get("props"))
    options = _normalize_options(spec.get("options"))

    if verb == "help":
        format_name = fmt or _normalize_optional_string(options.pop("format", None))
        format_name = str(format_name or "").strip().lower()
        if format_name not in SUPPORTED_FORMATS:
            raise ValueError('OfficeCLI help requires "format" (pptx/docx/xlsx).')
        topic = options.pop("topic", None) or options.pop("command_path", None)
        args = [format_name]
        if isinstance(topic, list):
            args.extend(str(item).strip() for item in topic if str(item).strip())
        elif topic is not None and str(topic).strip():
            args.extend(str(topic).strip().split())
        return args

    if verb == "create":
        if not file:
            raise ValueError('OfficeCLI create requires "file".')
        return [verb, file]

    if verb in {"open", "close", "validate", "watch", "unwatch"}:
        if not file:
            raise ValueError(f'OfficeCLI {verb} requires "file".')
        return [verb, file, *_compile_option_flags(options)]

    if verb == "view":
        if not file or not mode:
            raise ValueError('OfficeCLI view requires "file" and "mode".')
        return [verb, file, mode, *_compile_option_flags(options)]

    if verb == "get":
        if not file or not path:
            raise ValueError('OfficeCLI get requires "file" and "path".')
        return [verb, file, path, *_compile_option_flags(options)]

    if verb == "query":
        if not file or not selector:
            raise ValueError('OfficeCLI query requires "file" and "selector".')
        return [verb, file, selector, *_compile_option_flags(options)]

    if verb == "remove":
        if not file or not path:
            raise ValueError('OfficeCLI remove requires "file" and "path".')
        return [verb, file, path, *_compile_option_flags(options)]

    if verb == "set":
        if not file or not path:
            raise ValueError('OfficeCLI set requires "file" and "path".')
        if not props:
            raise ValueError('OfficeCLI set requires non-empty "props".')
        return [verb, file, path, *_compile_props(props), *_compile_option_flags(options)]

    if verb == "add":
        if not file or not parent or not content_type:
            raise ValueError('OfficeCLI add requires "file", "parent", and "type".')
        args = [verb, file, parent, "--type", content_type]
        if props:
            args.extend(_compile_props(props))
        args.extend(_compile_option_flags(options))
        return args

    raise ValueError(f"Unhandled OfficeCLI verb: {verb}")


def compile_officecli_command(spec: dict[str, Any]) -> str:
    return shlex.join(["officecli", *compile_officecli_argv(spec)])


def _render_officecli_command(spec: dict[str, Any]) -> str:
    normalized = _normalize_structured_spec(spec)
    verb = _normalize_optional_string(normalized.get("verb"))
    if not verb:
        return "officecli"

    file = _normalize_optional_string(normalized.get("file"))
    path = _normalize_optional_string(normalized.get("path"))
    parent = _normalize_optional_string(normalized.get("parent"))
    content_type = _normalize_optional_string(normalized.get("type"))
    mode = _normalize_optional_string(normalized.get("mode"))
    selector = _normalize_optional_string(normalized.get("selector"))
    fmt = _normalize_optional_string(normalized.get("format"))
    props = _normalize_props(normalized.get("props"))
    options = _normalize_options(normalized.get("options"))

    args = [verb]
    if verb == "help":
        format_name = fmt or _normalize_optional_string(options.pop("format", None))
        topic = options.pop("topic", None) or options.pop("command_path", None)
        if format_name:
            args.append(format_name)
        if isinstance(topic, list):
            args.extend(str(item).strip() for item in topic if str(item).strip())
        elif topic is not None and str(topic).strip():
            args.extend(str(topic).strip().split())
        return shlex.join(["officecli", *args])

    if file:
        args.append(file)

    if verb == "view" and mode:
        args.append(mode)
    elif verb in {"get", "remove", "set"} and path:
        args.append(path)
    elif verb == "query" and selector:
        args.append(selector)
    elif verb == "add":
        if parent:
            args.append(parent)
        if content_type:
            args.extend(["--type", content_type])

    if verb == "set" and props:
        args.extend(_compile_props(props))
    elif verb == "add" and props:
        args.extend(_compile_props(props))

    args.extend(_compile_option_flags(options))
    return shlex.join(["officecli", *args])


async def execute_officecli_spec(spec: dict[str, Any]) -> dict[str, Any]:
    constraints = get_office_constraints()
    normalized_spec = _normalize_structured_spec(spec, constraints=constraints)
    runtime_target = str(constraints.get("runtime_target", infer_office_runtime_target()) or "server")
    command = _render_officecli_command(normalized_spec)
    task_scope = _office_task_scope()
    blocked = _blocked_repeated_fatal_command(task_scope, command, runtime_target)
    if blocked is not None:
        return blocked
    spec_error = _validate_structured_spec(normalized_spec, runtime_target=runtime_target)
    if spec_error is not None:
        _remember_fatal_command(task_scope, command, spec_error)
        return spec_error

    raw_result = await _execute_officecli_spec_via_gateway(normalized_spec)
    if raw_result is None:
        command = compile_officecli_command(normalized_spec)
        raw_result = await _run_officecli_spec_locally(normalized_spec)
    else:
        # Desktop-routed commands may already have run successfully with paths that
        # are meaningful only on the client machine, so avoid re-validating them here.
        command = _render_officecli_command(normalized_spec)
    payload = _classify_officecli_result(command=command, **raw_result)
    if payload.get("kind") == "fatal_error":
        _remember_fatal_command(task_scope, command, payload)
    elif payload.get("success"):
        _clear_fatal_command(task_scope, command)
    return payload


async def execute_officecli_specs(specs: list[dict[str, Any]]) -> dict[str, Any]:
    constraints = get_office_constraints()
    results = [await execute_officecli_spec(_normalize_structured_spec(spec, constraints=constraints)) for spec in specs]
    overall_success, overall_kind = _aggregate_batch_kind(results)
    runtime_target = str(results[-1].get("runtime_target", "server") if results else "server")
    artifacts: list[dict[str, Any]] = []
    for result in results:
        for artifact in result.get("artifacts", []) or []:
            if isinstance(artifact, dict):
                artifacts.append(dict(artifact))
    return {
        "success": overall_success,
        "command": "officecli_batch",
        "exit_status": 0 if overall_success else 1,
        "kind": overall_kind,
        "message": "All commands completed successfully." if overall_success else f"Batch finished with {overall_kind}.",
        "results": results,
        "runtime_target": runtime_target,
        "artifacts": artifacts,
    }


async def execute_officecli_raw(command: str) -> dict[str, Any]:
    rejected = _reject_invalid_officecli_command(command)
    if rejected is not None:
        return rejected
    raw_result = await _execute_officecli_raw_via_gateway(command)
    if raw_result is None:
        raw_result = await _run_officecli_raw_locally(command)
    return _classify_officecli_result(command=command, **raw_result)


async def execute_officecli_raw_batch(commands: list[str]) -> dict[str, Any]:
    rejected_results = []
    for cmd in commands:
        rejected = _reject_invalid_officecli_command(cmd)
        if rejected is not None:
            rejected_results.append(rejected)
    if rejected_results:
        return {
            "success": False,
            "command": "officecli_batch",
            "exit_status": 1,
            "kind": "fatal_error",
            "message": "Batch rejected due to invalid OfficeCLI command input.",
            "results": rejected_results,
            "runtime_target": infer_office_runtime_target(),
            "artifacts": [],
        }

    results = [await execute_officecli_raw(cmd) for cmd in commands]
    overall_success, overall_kind = _aggregate_batch_kind(results)
    return {
        "success": overall_success,
        "command": "officecli_batch",
        "exit_status": 0 if overall_success else 1,
        "kind": overall_kind,
        "message": "All commands completed successfully." if overall_success else f"Batch finished with {overall_kind}.",
        "results": results,
        "runtime_target": str(results[-1].get("runtime_target", "server") if results else infer_office_runtime_target()),
        "artifacts": [],
    }


async def run(input_data) -> dict[str, Any]:
    payload = await _dispatch_legacy_input(input_data)
    return _legacy_result(payload)


async def run_batch(input_data) -> dict[str, Any]:
    payload = await _dispatch_legacy_batch_input(input_data)
    return _legacy_result(payload)


def get_officecli_skill_bundle(
    goal: str,
    file_hint: str | None = None,
    format_hint: str | None = None,
    operation_hint: str | None = None,
) -> str:
    return build_officecli_skill_bundle(
        goal,
        file_hint=file_hint,
        format_hint=format_hint,
        operation_hint=operation_hint,
    )


def _legacy_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok" if payload.get("success") else "error",
        "result": json.dumps(payload, ensure_ascii=False),
        "payload": payload,
    }


async def _dispatch_legacy_input(input_data: Any) -> dict[str, Any]:
    if isinstance(input_data, str):
        return await execute_officecli_raw(input_data)

    if not isinstance(input_data, dict):
        return await execute_officecli_raw(str(input_data))

    normalized = _normalize_structured_spec(input_data)
    if "verb" in normalized:
        return await execute_officecli_spec(normalized)

    command = input_data.get("command")
    if isinstance(command, str) and (" " in command or len(input_data) == 1):
        return await execute_officecli_raw(command)

    return await execute_officecli_raw(str(input_data))


async def _dispatch_legacy_batch_input(input_data: Any) -> dict[str, Any]:
    if isinstance(input_data, list):
        commands = input_data
        default_file = None
    elif isinstance(input_data, dict):
        commands = input_data.get("commands", [])
        default_file = _normalize_optional_string(input_data.get("file"))
    else:
        return await execute_officecli_raw_batch([str(input_data)])

    if not isinstance(commands, list) or not commands:
        return {
            "success": False,
            "command": "officecli_batch",
            "exit_status": 1,
            "kind": "fatal_error",
            "message": "Batch requires a non-empty commands list.",
            "results": [],
            "runtime_target": infer_office_runtime_target(),
            "artifacts": [],
        }

    if all(isinstance(item, str) for item in commands):
        return await execute_officecli_raw_batch([str(item) for item in commands])

    structured_commands = []
    for item in commands:
        if not isinstance(item, dict):
            return await execute_officecli_raw_batch([str(cmd) for cmd in commands])
        normalized = _normalize_structured_spec(item)
        if default_file and "file" not in normalized:
            normalized["file"] = default_file
        structured_commands.append(normalized)
    return await execute_officecli_specs(structured_commands)


async def _execute_officecli_spec_via_gateway(spec: dict[str, Any]) -> dict[str, Any] | None:
    configurable = _get_configurable()
    runtime_target = infer_office_runtime_target(configurable)
    tool_gateway = configurable.get("tool_gateway")
    user_id = str(configurable.get("request_user_id", "") or "")
    task_id = str(configurable.get("thread_id", "") or "")
    if runtime_target != "desktop" or tool_gateway is None or not user_id or not task_id:
        return None

    tool_gateway.set_route("officecli", "desktop")
    result = await tool_gateway.execute(
        ToolCall(
            tool_name="officecli",
            params={
                **_spec_to_gateway_params(spec),
                "_cost_stage": str(configurable.get("office_cost_stage", "") or "build"),
            },
            task_id=task_id,
            timeout_ms=_office_gateway_timeout_ms(spec),
        ),
        ToolContext(user_id=user_id, task_id=task_id, trace_id=task_id),
    )
    return {
        "success": result.success,
        "stdout": result.output or "",
        "stderr": result.error or "",
        "exit_status": 0 if result.success else 1,
        "runtime_target": runtime_target,
        "artifacts": _normalize_gateway_artifacts(result.artifacts, runtime_target),
    }


async def _execute_officecli_raw_via_gateway(command: str) -> dict[str, Any] | None:
    configurable = _get_configurable()
    runtime_target = infer_office_runtime_target(configurable)
    tool_gateway = configurable.get("tool_gateway")
    user_id = str(configurable.get("request_user_id", "") or "")
    task_id = str(configurable.get("thread_id", "") or "")
    if runtime_target != "desktop" or tool_gateway is None or not user_id or not task_id:
        return None

    tool_gateway.set_route("officecli", "desktop")
    result = await tool_gateway.execute(
        ToolCall(
            tool_name="officecli",
            params={
                "command": command,
                "_cost_stage": str(configurable.get("office_cost_stage", "") or "build"),
            },
            task_id=task_id,
            timeout_ms=_office_gateway_timeout_ms({"command": command}),
        ),
        ToolContext(user_id=user_id, task_id=task_id, trace_id=task_id),
    )
    return {
        "success": result.success,
        "stdout": result.output or "",
        "stderr": result.error or "",
        "exit_status": 0 if result.success else 1,
        "runtime_target": runtime_target,
        "artifacts": _normalize_gateway_artifacts(result.artifacts, runtime_target),
    }


async def _run_officecli_spec_locally(spec: dict[str, Any]) -> dict[str, Any]:
    argv = ["officecli", *compile_officecli_argv(spec)]
    result = subprocess.run(argv, capture_output=True, text=True)
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "exit_status": result.returncode,
        "runtime_target": "server",
        "artifacts": [],
    }


async def _run_officecli_raw_locally(command: str) -> dict[str, Any]:
    result = subprocess.run(["officecli", *shlex.split(command)], capture_output=True, text=True)
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "exit_status": result.returncode,
        "runtime_target": "server",
        "artifacts": [],
    }


def _spec_to_gateway_params(spec: dict[str, Any]) -> dict[str, Any]:
    spec = _normalize_structured_spec(spec)
    params = {
        "operation": str(spec.get("verb") or "").strip().lower(),
    }
    for key in ("file", "format", "path", "parent", "type", "mode", "selector", "props", "options"):
        if spec.get(key) is not None:
            params[key] = spec[key]
    if spec.get("commands") is not None:
        params["commands"] = spec["commands"]
    return params


def _normalize_optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_props(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items() if str(k).strip()}
    return {}


def _normalize_options(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items() if str(k).strip()}
    return {}


def _compile_props(props: dict[str, Any]) -> list[str]:
    args: list[str] = []
    for key, value in props.items():
        args.extend(["--prop", f"{key}={_stringify_value(value)}"])
    return args


def _compile_option_flags(options: dict[str, Any]) -> list[str]:
    args: list[str] = []
    for key, value in options.items():
        flag = f"--{str(key).replace('_', '-')}"
        if isinstance(value, bool):
            if value:
                args.append(flag)
            continue
        if value is None:
            continue
        if isinstance(value, list):
            for item in value:
                args.extend([flag, _stringify_value(item)])
            continue
        args.extend([flag, _stringify_value(value)])
    return args


def _stringify_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


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
