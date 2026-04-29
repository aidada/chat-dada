from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

OFFICE_WRITE_OPERATIONS = {"create", "edit", "transform"}
_JSON_OBJECT_WITH_ARTIFACTS_RE = re.compile(r"\{[^{}]*\"artifacts\"[^{}]*\}", re.DOTALL)


def coerce_office_operation(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"create", "edit", "inspect", "transform"}:
        return text
    return "create"


def is_write_operation(operation: Any) -> bool:
    return coerce_office_operation(operation) in OFFICE_WRITE_OPERATIONS


def extract_office_result_json(text: str) -> dict[str, Any] | None:
    """Extract the final structured Office JSON from the agent response."""
    if "```json" in text:
        chunks = text.split("```json")
        for chunk in chunks[1:]:
            candidate = chunk.split("```", 1)[0].strip()
            if not candidate:
                continue
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload

    match = _JSON_OBJECT_WITH_ARTIFACTS_RE.search(text)
    if match:
        try:
            payload = json.loads(match.group())
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return payload

    return None


def infer_office_format(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    suffix = Path(text).suffix.lower().lstrip(".")
    if suffix in {"pptx", "docx", "xlsx"}:
        return suffix
    lowered = text.lower()
    if lowered in {"pptx", "docx", "xlsx"}:
        return lowered
    return None


def normalize_result_artifacts(artifacts: Any) -> list[dict[str, Any]]:
    if not isinstance(artifacts, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            continue

        entry = {str(key): value for key, value in item.items() if value not in (None, "")}
        filename = str(entry.get("filename", "") or "").strip()
        path = str(entry.get("path", "") or "").strip()
        name = str(entry.get("name", "") or filename or Path(path).name).strip()
        if not name and not path:
            continue

        if name:
            entry["name"] = name
        if filename:
            entry["filename"] = filename
        if path:
            entry["path"] = path

        format_name = str(entry.get("format", "") or "").strip().lower() or infer_office_format(path or filename or name)
        if format_name:
            entry["format"] = format_name
        normalized.append(entry)

    return normalized

