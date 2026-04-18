"""Office domain orchestrated entrypoint."""
from __future__ import annotations

from copy import deepcopy
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent.workflows.office.result_utils import (
    coerce_office_operation,
    extract_office_result_json,
    infer_office_format,
    is_write_operation,
    normalize_result_artifacts,
)
from agent.workflows.office.workflow import (
    OFFICE_INNER_RECURSION_LIMIT,
    OFFICE_MAX_COST,
    OFFICE_MAX_STEPS,
    build_office_workflow_graph,
)
from agent.platform.streaming import stream_nested_graph
from agent.runtime.cost_logging import (
    append_stage_record,
    attach_partial_progress,
    attach_quality_summary,
    init_cost_ledger,
    log_cost_record,
    summarize_cost_ledger,
    update_completed_pages,
)
from agent.workflows.office.core.quality_report import summarize_quality_report
from agent.tools.officecli import ALLOWED_DIR, execute_officecli_spec, infer_office_runtime_target

_log = logging.getLogger("chatdada.office.orchestrated")


class OfficeDomainResult(BaseModel):
    status: str
    result: str
    artifact_refs: list[dict[str, Any]]
    review: dict[str, Any]
    budget: dict[str, Any]


_graph = build_office_workflow_graph()


from agent.platform.emit import safe_emit_progress_with_content as _safe_emit


def _merge_quality_report_summary(
    quality_report: dict[str, Any] | None,
    *summary_sources: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = summarize_quality_report(quality_report)
    for source in summary_sources:
        if isinstance(source, dict):
            merged.update({key: value for key, value in source.items() if value is not None})
    return merged


def _collect_source_files(input_data: dict[str, Any]) -> list[str]:
    raw = input_data.get("source_files")
    if raw is None:
        raw = input_data.get("file_paths")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _collect_reference_files(input_data: dict[str, Any]) -> list[str]:
    raw = input_data.get("reference_files")
    if raw is None:
        raw = input_data.get("reference_file_paths")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _expose_image_local_dirs(source_files: list[str], reference_files: list[str]) -> None:
    """把用户提供的本地素材所在目录暴露给 list_local_images 工具。

    把每个来源文件的父目录写入 OFFICE_IMAGE_LOCAL_DIRS（os.pathsep 分隔），
    image_tools.list_local_images 会读取这个变量去扫描可配图的本地素材。
    """
    seen: set[str] = set()
    dirs: list[str] = []
    for raw in [*source_files, *reference_files]:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            parent = Path(text).expanduser().resolve(strict=False).parent
        except Exception:
            continue
        key = str(parent)
        if key in seen:
            continue
        seen.add(key)
        dirs.append(key)
    if dirs:
        os.environ["OFFICE_IMAGE_LOCAL_DIRS"] = os.pathsep.join(dirs)
    else:
        os.environ.pop("OFFICE_IMAGE_LOCAL_DIRS", None)


def _snapshot_outputs(outputs_dir: Path) -> dict[str, int]:
    snapshot: dict[str, int] = {}
    for ext in ("*.pptx", "*.docx", "*.xlsx"):
        for path in outputs_dir.glob(ext):
            try:
                snapshot[str(path.resolve())] = path.stat().st_mtime_ns
            except OSError:
                continue
    return snapshot


def _detect_changed_outputs(before: dict[str, int], outputs_dir: Path) -> list[Path]:
    changed: list[Path] = []
    for ext in ("*.pptx", "*.docx", "*.xlsx"):
        for path in outputs_dir.glob(ext):
            try:
                resolved = str(path.resolve())
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            if resolved not in before or before[resolved] != mtime:
                changed.append(path)
    return sorted(changed, key=lambda item: item.stat().st_mtime, reverse=True)


def _build_server_artifact_ref(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    format_name = infer_office_format(path.name) or ""
    ref = {
        "name": path.name,
        "type": "file",
        "format": format_name,
        "role": "primary",
        "location": "server",
        "path": str(resolved),
        "display_path": str(resolved),
    }
    if resolved.parent == ALLOWED_DIR and path.exists():
        ref["url"] = f"/download/{path.name}"
    return ref


def _normalize_artifact_ref(item: dict[str, Any], *, runtime_target: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None

    entry = dict(item)
    filename = str(entry.get("filename", "") or "").strip()
    path_text = str(entry.get("path", "") or "").strip()
    name = str(entry.get("name", "") or filename or Path(path_text).name).strip()
    if not name and not path_text:
        return None

    location = str(entry.get("location", "") or runtime_target).strip().lower() or runtime_target
    display_path = str(entry.get("display_path", "") or path_text or name).strip()
    format_name = str(entry.get("format", "") or infer_office_format(path_text or filename or name) or "").strip()

    ref: dict[str, Any] = {
        "name": name or Path(path_text).name,
        "type": str(entry.get("type", "") or "file"),
        "location": location,
        "role": str(entry.get("role", "") or "primary"),
        "display_path": display_path,
    }
    if format_name:
        ref["format"] = format_name
    if filename:
        ref["filename"] = filename
    if path_text:
        ref["path"] = path_text
    if entry.get("url"):
        ref["url"] = entry["url"]

    if location == "server" and "url" not in ref:
        candidate = None
        if path_text:
            candidate = Path(path_text).expanduser().resolve(strict=False)
        elif filename:
            candidate = (ALLOWED_DIR / Path(filename).name).resolve()
            ref.setdefault("path", str(candidate))
            ref.setdefault("display_path", str(candidate))
        if candidate is not None and candidate.parent == ALLOWED_DIR and candidate.exists():
            ref["url"] = f"/download/{candidate.name}"

    return ref


def _resolve_artifact_refs(
    result_meta: dict[str, Any] | None,
    *,
    runtime_target: str,
    before_snapshot: dict[str, int],
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if result_meta is not None:
        for artifact in normalize_result_artifacts(result_meta.get("artifacts")):
            normalized = _normalize_artifact_ref(artifact, runtime_target=runtime_target)
            if normalized is not None:
                refs.append(normalized)

    if refs or runtime_target != "server":
        return refs

    for changed in _detect_changed_outputs(before_snapshot, ALLOWED_DIR):
        refs.append(_build_server_artifact_ref(changed))
    return refs


def _render_result_text(
    *,
    result_meta: dict[str, Any] | None,
    artifact_refs: list[dict[str, Any]],
    fallback_text: str,
) -> str:
    summary = str((result_meta or {}).get("summary", "") or "").strip() or fallback_text.strip()
    lines = [summary] if summary else []
    for ref in artifact_refs:
        if ref.get("location") == "desktop":
            lines.append(f"本地文件: {ref.get('display_path') or ref.get('name')}")
        elif ref.get("url"):
            lines.append(f"下载: {ref['url']}")
        elif ref.get("path"):
            lines.append(f"文件: {ref['path']}")
    return "\n".join(line for line in lines if line).strip() or fallback_text


def _artifact_close_candidates(ref: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for key in ("path", "filename", "name"):
        text = str(ref.get(key, "") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        candidates.append(text)
    return candidates


def _format_close_failure(target: str, payload: dict[str, Any]) -> str:
    message = str(payload.get("message", "") or payload.get("raw_stderr", "") or payload.get("raw_stdout", "") or "close failed").strip()
    command = str(payload.get("command", "") or f"officecli close {target}").strip()
    return f"{command}: {message}"


async def _flush_write_artifacts(artifact_refs: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    closed_targets: set[str] = set()

    for ref in artifact_refs:
        candidates = [item for item in _artifact_close_candidates(ref) if item not in closed_targets]
        if not candidates:
            continue

        artifact_failures: list[str] = []
        closed = False
        for target in candidates:
            payload = await execute_officecli_spec({"verb": "close", "file": target})
            if bool(payload.get("success")):
                closed_targets.add(target)
                closed = True
                break
            artifact_failures.append(_format_close_failure(target, payload))

        if not closed:
            failures.extend(artifact_failures or [f"officecli close {candidates[0]}: close failed"])

    return failures


async def run_office_domain_orchestrated(input_data: dict[str, Any]) -> OfficeDomainResult:
    query = input_data.get("query") or input_data.get("task", "")
    task_id = input_data.get("task_id", "office_unknown")
    source_files = _collect_source_files(input_data)
    reference_files = _collect_reference_files(input_data)
    try:
        from langgraph.config import get_config

        configurable = get_config().get("configurable", {}) or {}
    except Exception:
        configurable = {}

    runtime_target = infer_office_runtime_target(configurable)
    before_snapshot = _snapshot_outputs(ALLOWED_DIR) if runtime_target == "server" else {}
    _expose_image_local_dirs(source_files, reference_files)
    _log.info("Starting Office workflow: query=%s task_id=%s", str(query)[:60], task_id)
    finalize_started_at = time.perf_counter()
    _safe_emit("step", "Office task started...")

    initial_state: dict[str, Any] = {
        "goal": str(query),
        "task_id": str(task_id),
        "report_profile": "",
        "format_hint": str(input_data.get("format_hint", "") or ""),
        "file_hint": str(input_data.get("file_hint", "") or ""),
        "source_files": source_files,
        "reference_files": reference_files,
        "operation_hint": str(input_data.get("operation_hint", "") or ""),
        "cost": 0.0,
        "progress": 0.0,
        "confidence": 0.0,
        "max_cost": OFFICE_MAX_COST,
        "max_steps": OFFICE_MAX_STEPS,
        "inner_recursion_limit": OFFICE_INNER_RECURSION_LIMIT,
        "intermediate_results": [],
        "evaluations": [],
        "step_history": [],
        "coverage": {},
    }
    for key in (
        "goal_constraints",
        "reference_structure_constraints",
        "reference_style_constraints",
        "existing_document_profile",
        "fidelity_deviations",
    ):
        if key in input_data and input_data.get(key) is not None:
            initial_state[key] = deepcopy(input_data.get(key))

    result = await stream_nested_graph(
        _graph,
        initial_state,
        config={
            "configurable": {
                "thread_id": str(task_id),
                "office_constraints": {
                    "allowed_source_files": [*source_files, *reference_files],
                    "allowed_output_dir": str(ALLOWED_DIR),
                    "runtime_target": runtime_target,
                },
            }
        },
        extra_payload={
            "nested_graph": "office_workflow",
            "domain_name": "office",
            "source": "office_workflow",
        },
    )

    content_text = str(result.get("final_result", "") or "")
    strategy_trace = result.get("step_history", []) or []
    strategies_used = [str(item.get("strategy", "") or "") for item in strategy_trace]
    terminal_status = str(result.get("terminal_status", "") or "")
    terminal_reason = str(result.get("terminal_reason", terminal_status) or terminal_status)
    cost_ledger = dict(result.get("cost_ledger") or {})
    quality_report = dict(result.get("quality_report") or {})
    quality_report_summary = _merge_quality_report_summary(
        quality_report,
        result.get("quality_report_summary") if isinstance(result.get("quality_report_summary"), dict) else None,
        cost_ledger.get("quality_report_summary") if isinstance(cost_ledger.get("quality_report_summary"), dict) else None,
    )
    partial_progress = dict(result.get("partial_progress") or {})
    if not cost_ledger:
        cost_ledger = init_cost_ledger(
            task_id=str(task_id),
            domain="office",
            metadata={"runtime_target": runtime_target},
        )
    cost_ledger = attach_quality_summary(
        cost_ledger,
        quality_report_summary=quality_report_summary,
    )
    cost_ledger = attach_partial_progress(cost_ledger, partial_progress=partial_progress)

    if not content_text:
        cost_ledger = append_stage_record(
            cost_ledger,
            stage="finalize",
            status="error",
            elapsed_ms=int((time.perf_counter() - finalize_started_at) * 1000),
            metadata={"reason": "no_content_generated"},
        )
        log_cost_record("task_summary", summarize_cost_ledger(cost_ledger))
        return OfficeDomainResult(
            status="error",
            result="Office 任务失败：agent 未返回结果。",
            artifact_refs=[],
            review={
                "passed": False,
                "reason": "No content generated",
                "quality_report": quality_report,
                "quality_report_summary": quality_report_summary,
                "partial_progress": partial_progress,
            },
            budget={
                "action": "allow",
                "reason": f"workflow({' → '.join(strategies_used)})",
                "quality_report_summary": quality_report_summary,
                "cost_ledger": summarize_cost_ledger(cost_ledger),
            },
        )

    if terminal_status:
        if partial_progress:
            completed_pages = int(partial_progress.get("completed_pages", 0) or 0)
            cost_ledger = update_completed_pages(cost_ledger, completed_pages=completed_pages)
        cost_ledger = append_stage_record(
            cost_ledger,
            stage="finalize",
            status="blocked",
            elapsed_ms=int((time.perf_counter() - finalize_started_at) * 1000),
            metadata={
                "terminal_status": terminal_status,
                "terminal_reason": terminal_reason,
                "partial_progress": partial_progress,
            },
        )
        log_cost_record("task_summary", summarize_cost_ledger(cost_ledger))
        detail_lines = [content_text]
        if partial_progress.get("completed_pages"):
            detail_lines.append(f"已完成页数: {int(partial_progress['completed_pages'])}")
        if partial_progress.get("current_batch_slide_range"):
            start, end = partial_progress["current_batch_slide_range"]
            detail_lines.append(f"当前批次: slide {start}-{end}")
        if partial_progress.get("reason"):
            detail_lines.append(f"中止原因: {partial_progress['reason']}")
        return OfficeDomainResult(
            status="error",
            result="\n".join(line for line in detail_lines if line),
            artifact_refs=[],
            review={
                "passed": False,
                "reason": terminal_reason,
                "quality_report": quality_report,
                "quality_report_summary": quality_report_summary,
                "partial_progress": partial_progress,
            },
            budget={
                "action": "allow",
                "reason": f"workflow({' → '.join(strategies_used)})",
                "quality_report_summary": quality_report_summary,
                "cost_ledger": summarize_cost_ledger(cost_ledger),
            },
        )

    result_meta = extract_office_result_json(content_text)
    if isinstance(result_meta, dict):
        result_stats = result_meta.get("stats")
        if isinstance(result_stats, dict) and result_stats:
            merged_quality_summary = dict(quality_report_summary)
            for key in (
                "section_count",
                "sheet_count",
                "slide_count",
                "content_slide_count",
                "notes_slide_count",
                "transition_slide_count",
                "visual_slide_count",
                "text_only_slide_count",
                "layout_variety_count",
                "picture_count",
                "chart_count",
                "table_count",
                "qa_checks",
            ):
                if merged_quality_summary.get(key) is None and result_stats.get(key) is not None:
                    merged_quality_summary[key] = result_stats.get(key)
            if merged_quality_summary != quality_report_summary:
                quality_report_summary = merged_quality_summary
                cost_ledger = attach_quality_summary(
                    cost_ledger,
                    quality_report_summary=quality_report_summary,
                )
    artifact_refs = _resolve_artifact_refs(
        result_meta,
        runtime_target=runtime_target,
        before_snapshot=before_snapshot,
    )

    operation = coerce_office_operation((result_meta or {}).get("operation") or input_data.get("operation_hint"))
    validated = bool((result_meta or {}).get("validated", False))
    passed = not is_write_operation(operation) or validated
    if is_write_operation(operation) and not artifact_refs:
        passed = False

    if is_write_operation(operation) and validated and artifact_refs:
        flush_failures = await _flush_write_artifacts(artifact_refs)
        if flush_failures:
            cost_ledger = append_stage_record(
                cost_ledger,
                stage="finalize",
                status="error",
                elapsed_ms=int((time.perf_counter() - finalize_started_at) * 1000),
                metadata={"flush_failures": flush_failures},
            )
            log_cost_record("task_summary", summarize_cost_ledger(cost_ledger))
            failure_text = (
                "Office 任务失败：文档内容已生成并通过 validate，但最终 close/flush 失败。\n"
                + "\n".join(flush_failures)
            )
            return OfficeDomainResult(
                status="error",
                result=failure_text,
                artifact_refs=artifact_refs,
                review={
                    "passed": False,
                    "reason": "Office close/flush failed",
                    "operation": operation,
                    "runtime_target": runtime_target,
                    "close_failures": flush_failures,
                    "quality_report": quality_report,
                    "quality_report_summary": quality_report_summary,
                },
                budget={
                    "action": "allow",
                    "reason": f"workflow({' → '.join(strategies_used)})",
                    "quality_report_summary": quality_report_summary,
                    "cost_ledger": summarize_cost_ledger(cost_ledger),
                },
            )

    for ref in artifact_refs:
        payload = {
            "type": "file",
            "name": ref.get("name"),
            "location": ref.get("location"),
        }
        if ref.get("url"):
            payload["url"] = ref["url"]
        if ref.get("path"):
            payload["path"] = ref["path"]
        _safe_emit("file", json.dumps(payload, ensure_ascii=False))

    result_text = _render_result_text(
        result_meta=result_meta,
        artifact_refs=artifact_refs,
        fallback_text=content_text,
    )
    completed_pages = 0
    if (result_meta or {}).get("stats") and isinstance(result_meta.get("stats"), dict):
        try:
            stats = result_meta["stats"]
            completed_pages = int(
                stats.get("slide_count", 0)
                or stats.get("sheet_count", 0)
                or stats.get("section_count", 0)
                or 0
            )
        except (TypeError, ValueError):
            completed_pages = 0
    if completed_pages:
        cost_ledger = update_completed_pages(cost_ledger, completed_pages=completed_pages)
    cost_ledger = append_stage_record(
        cost_ledger,
        stage="finalize",
        status="ok" if passed else "partial",
        elapsed_ms=int((time.perf_counter() - finalize_started_at) * 1000),
        metadata={
            "operation": operation,
            "runtime_target": runtime_target,
            "artifact_count": len(artifact_refs),
        },
    )
    log_cost_record("task_summary", summarize_cost_ledger(cost_ledger))
    return OfficeDomainResult(
        status="ok",
        result=result_text,
        artifact_refs=artifact_refs,
        review={
            "passed": passed,
            "reason": "Office task completed" if passed else "Office task missing validated artifacts",
            "operation": operation,
            "runtime_target": runtime_target,
            "quality_report": quality_report,
            "quality_report_summary": quality_report_summary,
        },
        budget={
            "action": "allow",
            "reason": f"workflow({' → '.join(strategies_used)})",
            "quality_report_summary": quality_report_summary,
            "cost_ledger": summarize_cost_ledger(cost_ledger),
        },
    )
