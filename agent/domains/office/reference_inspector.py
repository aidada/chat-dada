from __future__ import annotations

import json
from typing import Any

from agent.tools.officecli import execute_officecli_spec


def _unwrap_officecli_result(result: dict[str, Any], *, mode: str) -> Any:
    raw_stdout = str(result.get("raw_stdout", "") or "")
    raw_stderr = str(result.get("raw_stderr", "") or "")
    message = str(result.get("message", "") or "")

    if raw_stdout:
        try:
            parsed = json.loads(raw_stdout)
            if mode == "outline":
                if isinstance(parsed, list):
                    return parsed
                return []
            if mode == "stats":
                if isinstance(parsed, dict):
                    return parsed
                return {
                    "slide_count": 0,
                    "layout_variety_count": 0,
                    "message": message or raw_stderr,
                    "raw_stdout": raw_stdout,
                    "raw_stderr": raw_stderr,
                    "success": bool(result.get("success")),
                    "kind": str(result.get("kind") or ""),
                    "exit_status": result.get("exit_status"),
                }
            if mode in {"issues", "annotated"}:
                if isinstance(parsed, dict):
                    return {
                        "text": str(parsed.get("text", "") or ""),
                        "message": str(parsed.get("message", "") or message or ""),
                        "raw_stdout": raw_stdout,
                        "raw_stderr": raw_stderr,
                        "success": bool(result.get("success")),
                        "kind": str(result.get("kind") or ""),
                        "exit_status": result.get("exit_status"),
                    }
                if isinstance(parsed, str):
                    return {
                        "text": parsed,
                        "message": message or parsed,
                        "raw_stdout": raw_stdout,
                        "raw_stderr": raw_stderr,
                        "success": bool(result.get("success")),
                        "kind": str(result.get("kind") or ""),
                        "exit_status": result.get("exit_status"),
                    }
                return {
                    "text": message or raw_stderr,
                    "message": message or raw_stderr,
                    "raw_stdout": raw_stdout,
                    "raw_stderr": raw_stderr,
                    "success": bool(result.get("success")),
                    "kind": str(result.get("kind") or ""),
                    "exit_status": result.get("exit_status"),
                }
            if mode == "text":
                return parsed
        except json.JSONDecodeError:
            pass

    fallback_text = raw_stdout or message or raw_stderr
    if mode == "outline":
        return [{"title": fallback_text}] if fallback_text else []
    if mode == "stats":
        return {
            "slide_count": 0,
            "layout_variety_count": 0,
            "message": message or fallback_text,
            "raw_stdout": raw_stdout,
            "raw_stderr": raw_stderr,
            "success": bool(result.get("success")),
            "kind": str(result.get("kind") or ""),
            "exit_status": result.get("exit_status"),
        }
    if mode in {"issues", "annotated"}:
        return {
            "text": fallback_text,
            "message": message or fallback_text,
            "raw_stdout": raw_stdout,
            "raw_stderr": raw_stderr,
            "success": bool(result.get("success")),
            "kind": str(result.get("kind") or ""),
            "exit_status": result.get("exit_status"),
        }
    return fallback_text


async def inspect_reference_file(*, format_name: str, file_path: str) -> dict[str, Any]:
    normalized_format_name = str(format_name or "").strip().lower()
    if normalized_format_name == "pptx":
        outline = _unwrap_officecli_result(
            await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "outline"}),
            mode="outline",
        )
        stats = _unwrap_officecli_result(
            await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "stats"}),
            mode="stats",
        )
        return {"outline": outline, "stats": stats}
    if normalized_format_name == "xlsx":
        text = _unwrap_officecli_result(
            await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "text"}),
            mode="text",
        )
        issues = _unwrap_officecli_result(
            await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "issues"}),
            mode="issues",
        )
        return {"text": text, "issues": issues}
    text = _unwrap_officecli_result(
        await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "text"}),
        mode="text",
    )
    annotated = _unwrap_officecli_result(
        await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "annotated"}),
        mode="annotated",
    )
    return {"text": text, "annotated": annotated}
