"""
PPT domain agent — OfficeCLI version.

Uses domain-internal workflow for PPT tasks.
(PRD §8.3 C1: inlined build_orchestrated_graph to domain module)

The agent receives the officecli SKILL.md as system context and uses
officecli_run / officecli_batch tools to create .pptx files directly.
Research tools (web_search, etc.) are also available for content gathering.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent.domains.ppt.workflow import (
    build_ppt_workflow_graph,
    PPT_MAX_COST,
    PPT_MAX_STEPS,
)
from agent.platform.streaming import stream_nested_graph

_log = logging.getLogger("chatdada.ppt.orchestrated")


# ── Shared result model ─────────────────────────────────────────────────────

class PptDomainResult(BaseModel):
    status: str
    result: str
    artifact_refs: list[dict[str, Any]]
    review: dict[str, Any]
    budget: dict[str, Any]


# ── Compiled graph ───────────────────────────────────────────────────────────

_graph = build_ppt_workflow_graph()


# ── Helper functions ─────────────────────────────────────────────────────────

def _safe_emit(event_type: str, content: str | dict[str, Any]) -> None:
    try:
        from langgraph.config import get_stream_writer
        payload = dict(content) if isinstance(content, dict) else {"content": content}
        payload.setdefault("event_type", event_type)
        get_stream_writer()(payload)
    except Exception:
        pass


def _extract_result_json(text: str) -> dict | None:
    """Extract the {filename, title, slide_count} JSON from agent's final response."""
    if "```json" in text:
        try:
            json_str = text.split("```json")[1].split("```")[0].strip()
            return json.loads(json_str)
        except (json.JSONDecodeError, IndexError):
            pass
    # Fallback: search for JSON object with "filename" key
    import re
    m = re.search(r'\{[^}]*"filename"[^}]*\}', text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None


async def run_ppt_domain_orchestrated(
    input_data: dict[str, Any],
) -> PptDomainResult:
    """Run PPT domain using the domain-internal workflow."""
    query = input_data.get("query") or input_data.get("task", "")
    task_id = input_data.get("task_id", "ppt_unknown")

    _log.info("Starting PPT workflow: query=%s task_id=%s", str(query)[:60], task_id)
    _safe_emit("step", "PPT generation started...")

    result = await stream_nested_graph(
        _graph,
        {
            "goal": str(query),
            "task_id": str(task_id),
            "report_profile": "",
            "cost": 0.0,
            "progress": 0.0,
            "confidence": 0.0,
            "max_cost": PPT_MAX_COST,
            "max_steps": PPT_MAX_STEPS,
            "intermediate_results": [],
            "evaluations": [],
            "step_history": [],
            "coverage": {},
        },
        config={"configurable": {"thread_id": str(task_id)}},
        extra_payload={
            "nested_graph": "ppt_workflow",
            "domain_name": "ppt",
            "source": "ppt_workflow",
        },
    )

    content_text = result.get("final_result", "")
    strategy_trace = result.get("step_history", [])
    strategies_used = [s.get("strategy", "") for s in strategy_trace]

    if not content_text:
        return PptDomainResult(
            status="error",
            result="PPT 生成失败：agent 未返回结果。",
            artifact_refs=[],
            review={"passed": False, "reason": "No content generated"},
            budget={"action": "allow", "reason": f"workflow({' → '.join(strategies_used)})"},
        )

    # Extract filename from agent's structured JSON output
    result_meta = _extract_result_json(content_text)
    outputs_dir = Path(os.getenv("OFFICECLI_ALLOWED_DIR", "outputs")).resolve()

    if result_meta and result_meta.get("filename"):
        filename = Path(result_meta["filename"]).name  # prevent traversal
        title = result_meta.get("title", str(query)[:30])
        slide_count = result_meta.get("slide_count", "?")
        output_path = outputs_dir / filename

        if output_path.exists():
            _safe_emit("step", f"PPT created: {filename}")
            _safe_emit("file", json.dumps({"type": "file", "url": f"/download/{filename}", "name": filename}))

            result_text = f"PPT 已生成：《{title}》，共 {slide_count} 页。\n下载: /download/{filename}"
            return PptDomainResult(
                status="ok",
                result=result_text,
                artifact_refs=[{"name": filename, "type": "pptx", "url": f"/download/{filename}"}],
                review={"passed": True, "reason": "PPT created via OfficeCLI"},
                budget={"action": "allow", "reason": f"workflow({' → '.join(strategies_used)})"},
            )

    # Fallback: look for any .pptx file created during this run
    pptx_files = sorted(outputs_dir.glob("*.pptx"), key=lambda p: p.stat().st_mtime, reverse=True)
    if pptx_files:
        latest = pptx_files[0]
        filename = latest.name
        _safe_emit("step", f"PPT created: {filename}")
        _safe_emit("file", json.dumps({"type": "file", "url": f"/download/{filename}", "name": filename}))

        return PptDomainResult(
            status="ok",
            result=f"PPT 已生成：{filename}\n下载: /download/{filename}\n\n{content_text}",
            artifact_refs=[{"name": filename, "type": "pptx", "url": f"/download/{filename}"}],
            review={"passed": True, "reason": "PPT file found in outputs"},
            budget={"action": "allow", "reason": f"workflow({' → '.join(strategies_used)})"},
        )

    # No file produced — return raw content
    _log.warning("Agent produced text but no .pptx file was found")
    return PptDomainResult(
        status="ok",
        result=content_text,
        artifact_refs=[],
        review={"passed": False, "reason": "No .pptx file produced"},
        budget={"action": "allow", "reason": f"workflow({' → '.join(strategies_used)})"},
    )
