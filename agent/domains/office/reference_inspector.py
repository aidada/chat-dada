from __future__ import annotations

from typing import Any

from agent.tools.officecli import execute_officecli_spec


async def inspect_reference_file(*, format_name: str, file_path: str) -> dict[str, Any]:
    if format_name == "pptx":
        outline = await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "outline"})
        stats = await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "stats"})
        return {"outline": outline, "stats": stats}
    if format_name == "xlsx":
        text = await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "text"})
        issues = await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "issues"})
        return {"text": text, "issues": issues}
    text = await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "text"})
    annotated = await execute_officecli_spec({"verb": "view", "file": file_path, "mode": "annotated"})
    return {"text": text, "annotated": annotated}
