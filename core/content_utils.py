from __future__ import annotations

import re
from typing import Any

RESULT_TEXT_KEYS = ("result", "content", "text", "analysis", "findings", "summary", "message")
TEXT_BLOCK_TYPES = {"text", "output_text", "input_text"}


def extract_text_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts = [extract_text_content(item).strip() for item in value]
        return "\n\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return _extract_text_from_dict(value)

    content = getattr(value, "content", None)
    if content is not None:
        text = extract_text_content(content)
        if text:
            return text

    text_attr = getattr(value, "text", None)
    if isinstance(text_attr, str):
        return text_attr

    return str(value)


def extract_result_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in RESULT_TEXT_KEYS:
            if key not in value:
                continue
            text = extract_text_content(value.get(key)).strip()
            if text:
                return normalize_markdown_report(text)

    text = extract_text_content(value).strip()
    return normalize_markdown_report(text)


def normalize_markdown_report(text: str) -> str:
    if not text:
        return ""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    lines = normalized.split("\n")
    cleaned: list[str] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue

        bold_heading = re.fullmatch(r"\*\*(.+?)\*\*", line)
        if bold_heading:
            line = f"## {bold_heading.group(1).strip()}"

        cleaned.append(line)

    while cleaned and cleaned[-1] == "":
        cleaned.pop()

    return "\n".join(cleaned)


def _extract_text_from_dict(value: dict[str, Any]) -> str:
    block_type = str(value.get("type", "") or "")
    direct_text = value.get("text")
    if isinstance(direct_text, str) and (not block_type or block_type in TEXT_BLOCK_TYPES):
        return direct_text

    for key in RESULT_TEXT_KEYS:
        if key not in value:
            continue
        text = extract_text_content(value.get(key))
        if text:
            return text

    if block_type == "reasoning":
        return extract_text_content(value.get("summary"))

    return ""
