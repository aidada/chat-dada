"""
Office domain internal workflow.

Provides a lightweight LangGraph workflow around OfficeCLI-based document work.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from langchain_core.messages import AIMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph

from core.content_utils import extract_result_text
from langgraph.config import get_config
from agent.domains.office.core import OfficeWorkflowState, finalize_node, route_after_build, route_after_qa_fix
from agent.domains.office.builder import run_section_builder
from agent.domains.office.goal_normalizer import (
    extract_explicit_filename,
    infer_default_create_file,
    infer_requested_slide_count,
    normalize_goal_profile,
    refine_filename_from_plan,
)
from agent.domains.office.reference_resolver import resolve_reference_constraints
from agent.domains.office.qa import run_quality_gate
from agent.domains.office.result_utils import (
    coerce_office_operation,
    extract_office_result_json,
    is_write_operation,
)
from agent.domains.office.strategies import get_strategy_for_format
from agent.domains.research.tools import get_research_tools
from agent.tools.officecli import infer_office_runtime_target
from agent.runtime.cost_logging import append_stage_record, init_cost_ledger

_log = logging.getLogger("chatdada.office.workflow")

OFFICE_MODEL_ROLE = "orchestrator"
OFFICE_MAX_STEPS = 15
OFFICE_MAX_COST = 3.0
OFFICE_INNER_RECURSION_LIMIT = 40

_EDIT_HINTS = (
    "修改",
    "编辑",
    "更新",
    "改写",
    "润色",
    "replace",
    "update",
    "edit",
    "fix",
)
_INSPECT_HINTS = (
    "查看",
    "检查",
    "分析",
    "读取",
    "提取",
    "总结",
    "inspect",
    "review",
    "analyze",
    "read",
)
_TRANSFORM_HINTS = (
    "转换",
    "导出",
    "另存为",
    "转成",
    "convert",
    "export",
)
_CREATE_HINTS = (
    "创建",
    "生成",
    "制作",
    "做",
    "写",
    "draft",
    "create",
    "generate",
)
_PATH_LIKE_RE = re.compile(r"([A-Za-z]:\\|/|~|\.pptx\b|\.docx\b|\.xlsx\b)")
_EXPLICIT_FILENAME_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:pptx|docx|xlsx))\b", re.IGNORECASE)
_ASCII_FILENAME_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
_REQUESTED_SLIDE_COUNT_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:页|page(?:s)?|slide(?:s)?)(?!\w)", re.IGNORECASE)
_CONTENT_RESEARCHER_TOOL_NAMES = {
    "exa_deep_search",
    "academic_search",
    "web_search",
    "brave_search",
    "browser_navigate",
}
_FILENAME_STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "for",
    "to",
    "of",
    "in",
    "on",
    "with",
    "create",
    "generate",
    "make",
    "draft",
    "write",
    "ppt",
    "pptx",
    "doc",
    "docx",
    "xlsx",
    "excel",
    "word",
    "presentation",
    "document",
    "workbook",
    "download",
    "downloads",
    "folder",
    "file",
}
_INTENT_FILENAME_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("介绍", "intro", "overview"), "intro"),
    (("指南", "guide", "tutorial", "使用"), "guide"),
    (("总结", "summary"), "summary"),
    (("报告", "report"), "report"),
    (("方案", "proposal"), "proposal"),
    (("计划", "plan"), "plan"),
    (("分析", "analysis"), "analysis"),
    (("痛点", "pain point", "pain points"), "pain-points"),
    (("dashboard", "仪表盘", "kpi"), "dashboard"),
    (("research", "研究"), "research"),
    (("patent", "专利"), "patent"),
    (("financial", "finance", "财务"), "financial-model"),
    (("education", "learning", "study", "teaching", "教育", "学习"), "education"),
    (("children", "child", "kids", "student", "students", "孩子", "儿童", "青少年"), "children"),
    (("modern", "modernization", "new era", "新时代", "现代化"), "modern"),
)
_FORMAT_DEFAULT_FILENAMES = {
    "pptx": "presentation",
    "docx": "document",
    "xlsx": "workbook",
}
_GENERIC_FILENAME_STEMS = {
    "ai",
    "deck",
    "slides",
    "slide-deck",
    "presentation",
    "document",
    "workbook",
    "file",
    "output",
    "result",
    "demo",
    "temp",
    "untitled",
    "new",
}


@dataclass
class SubagentConfig:
    name: str
    description: str
    system_prompt: str
    tools: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "tools": self.tools,
        }


def _build_content_researcher_tools() -> list[Any]:
    return [
        tool
        for tool in get_research_tools()
        if getattr(tool, "name", "") in _CONTENT_RESEARCHER_TOOL_NAMES
    ]


OFFICE_SUBAGENTS = [
    SubagentConfig(
        name="content_researcher",
        description="Search for missing data or supporting material when the Office task requires external facts.",
        system_prompt="仅在 Office 任务缺少内容素材时进行外部搜索。输出结构化要点和来源。",
        tools=_build_content_researcher_tools(),
    ),
]
from agent.platform.emit import safe_emit_progress_with_content as _safe_emit


def _extract_last_ai_text(response: Any) -> str:
    messages = response.get("messages", []) if isinstance(response, dict) else []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = extract_result_text(getattr(msg, "content", ""))
            if text:
                return text
    return ""


def _build_subagent_dicts() -> list[dict[str, Any]]:
    return [s.to_dict() for s in OFFICE_SUBAGENTS]


# Backward-compatible helper aliases while normalization logic is migrated out of workflow.py.
_extract_explicit_filename = extract_explicit_filename
_infer_default_create_file = infer_default_create_file
_infer_requested_slide_count = infer_requested_slide_count


def _infer_format(goal: str, file_hint: str, source_files: list[str], explicit: str) -> str:
    explicit_lower = str(explicit or "").strip().lower()
    if explicit_lower in {"pptx", "docx", "xlsx"}:
        return explicit_lower

    candidates = [file_hint, *source_files]
    for item in candidates:
        suffix = Path(str(item or "")).suffix.lower().lstrip(".")
        if suffix in {"pptx", "docx", "xlsx"}:
            return suffix

    lowered = str(goal or "").lower()
    if any(keyword in lowered for keyword in ("ppt", "powerpoint", "presentation", "deck", "幻灯片", "演示文稿")):
        return "pptx"
    if any(keyword in lowered for keyword in ("docx", "word", "memo", "letter", "manuscript", "proposal", "报告")):
        return "docx"
    if any(keyword in lowered for keyword in ("xlsx", "excel", "spreadsheet", "workbook", "dashboard", "表格", "电子表格")):
        return "xlsx"
    return ""


def _infer_operation(goal: str, source_files: list[str], explicit: str) -> str:
    explicit_lower = str(explicit or "").strip().lower()
    if explicit_lower in {"create", "edit", "inspect", "transform"}:
        return explicit_lower

    lowered = str(goal or "").lower()
    if any(keyword in lowered for keyword in _TRANSFORM_HINTS):
        return "transform"
    if any(keyword in lowered for keyword in _EDIT_HINTS):
        return "edit"
    if any(keyword in lowered for keyword in _INSPECT_HINTS):
        return "inspect"
    if any(keyword in lowered for keyword in _CREATE_HINTS):
        return "create"
    return "inspect" if source_files else "create"


def _build_format_specific_guidance(
    *,
    goal: str,
    format_name: str,
    operation: str,
    requested_slide_count: int | None,
) -> str:
    if format_name != "pptx" or operation not in {"create", "transform"}:
        return ""

    if requested_slide_count is not None:
        slide_count_rule = f"- 用户明确要求约 {requested_slide_count} 页，控制在该范围内，同时保持完整叙事闭环。"
    else:
        slide_count_rule = "- 用户未明确页数时，默认规划 6-8 页；如果只是简短介绍类 deck，默认做成 5-6 页。"

    lowered_goal = str(goal or "").lower()
    if any(token in lowered_goal for token in ("介绍", "intro", "overview", "能力", "是什么", "what is", "capability")):
        storyline_hint = "- 介绍/概览类 deck 默认采用：封面 -> 为什么重要 -> 它是什么 -> 核心能力 -> 工作流/案例 -> 总结。"
    elif any(token in lowered_goal for token in ("培训", "教程", "guide", "how to", "流程", "步骤")):
        storyline_hint = "- 讲解/培训类 deck 默认采用：封面 -> 背景 -> 概念拆解 -> 步骤流程 -> 示例 -> 总结。"
    else:
        storyline_hint = "- 默认采用：封面 -> 背景/问题 -> 关键信息 -> 证据/流程/案例 -> 总结/下一步。"

    return f"""\
## PPT 创建质量门槛

- 在第一次 create/add 之前，先在内部完成逐页规划：每页的 `slide role`、核心结论、布局类型、视觉元素、speaker notes。
{slide_count_rule}
{storyline_hint}
- 先一次性规划完整 deck，再执行写入；不要每写完一页就重新思考整套 PPT。
- 当页数 >= 8 时，必须按 section 或每批 2-3 页进行批量写入，优先使用 `officecli_batch` 一次提交整批 slide 的骨架和主要内容。
- 优先先建立整套 slide skeleton，再分批填充内容、视觉元素、notes、transitions；不要把 create/add/validate 交织成细粒度来回循环。
- 每页必须先定义一句 takeaway headline，避免出现只有“核心功能/痛点/总结”这种空标题。
- 正文不要堆字：每个主内容区最多 4 个 bullet，或约 60-80 个中文字符；放不下就拆 slide。
- 每张内容 slide 必须有至少一个非文字视觉元素：卡片、色块、流程图、时间线、图表、表格、KPI 数字、对比栏、图片之一。
- 如果没有现成图片素材，不允许交付纯文字 deck；改用 shapes / cards / chart / process flow 做视觉表达。
- 至少使用 3 种布局模式（当 slide_count >= 3）：例如双栏、卡片网格、大数字、流程、对比。
- 不允许连续两张内容 slide 使用同一种布局结构。
- 第 2 张及之后的每张 slide 都要设置 transition；整套 deck 最多使用 2 种 transition 类型。优先 `fade`、`push-left`、`wipe-right`。
- 每张内容 slide 都必须有 speaker notes；封面/结尾页可以无 notes。

## PPT 交付前 QA（必须执行）

- 用结构化 `officecli(verb="view", mode="stats", file="...")` 检查页数与图片/图表等统计。
- 用结构化 `officecli(verb="view", mode="annotated", file="...")` 检查每张 slide 是否仍然只有 Text Box。
- 用结构化 `officecli(verb="validate", file="...")` 做最终校验。
- 如果 `annotated` 显示内容 slide 只有 Text Box，或 `stats` 显示 transition / notes / layout variety 不达标，必须修正后再交付。

## PPT 最终 JSON 的 stats 必填字段

- `slide_count`
- `content_slide_count`
- `notes_slide_count`
- `transition_slide_count`
- `visual_slide_count`
- `text_only_slide_count`
- `layout_variety_count`
- `picture_count`
- `chart_count`
- `table_count`
- `qa_checks`

其中 `qa_checks` 必须明确包含：`view_stats`、`view_annotated`、`validate`。
"""


_OFFICE_SYSTEM = """\
你是 Office 文档执行专家。你可以使用结构化 officecli / officecli_batch 工具处理 .pptx / .docx / .xlsx。

## 当前任务上下文

- 目标格式：{format_hint}
- 任务类型：{operation}
- 运行时：{runtime_target}
- 默认创建文件名：{default_create_file}
- 允许访问的源文件：
{source_files_block}

## 硬规则

- 优先按任务类型行动：已有文件先 inspect，再决定是否写入；不要默认直接改写。
- 只有在用户目标缺少内容素材、事实、数据时，才调用搜索类工具。
- 对 OfficeCLI 语法、DOM 路径、属性名或帮助主题不确定时，必须先调用 help/get/view/query，禁止猜测伪命令。
- 结构化 `officecli` / `officecli_batch` 调用一律使用 `verb` 字段；不要在工具参数里使用 `command`。如果参考手册里的原始 `officecli batch` JSON 示例出现 `command`，转换成结构化工具调用时必须改写为 `verb`。
- 只允许使用 OfficeCLI 工具；禁止编写或执行 Python、bash、shell 脚本来生成或修改 Office 文档。
- 如果发生写操作，完成后必须调用 validate；若 validate 未通过，不要宣称任务成功。
- 如果当前阶段明确说明“final validation deferred”，则本轮只允许完成指定 batch 的写入，不要提前做整套文档的最终 QA。
- 如果当前阶段明确说明“repair run”，则只针对给定 QA 问题做有限修复，然后执行完整 QA。
- 如果相同 command + kind + message 连续出现 2 次，停止重试并输出失败总结。
- 一旦收到 kind=fatal_error，立即停止修复并输出失败总结。
- 文档文件名只能是英文字母、数字、下划线、短横线和扩展名。
- create 时只传文件名，不要自行构造服务端路径。
- 如果用户没有明确指定文件名，而任务类型是 create，必须使用上面的默认创建文件名；不要传空 file，也不要传 null。
- inspect 任务可以不产出 artifact，但 create/edit/transform 必须在最终 JSON 中列出 artifacts。
- 最终回复必须包含一个 ```json 代码块，结构如下：
```json
{{
  "operation": "create|edit|inspect|transform",
  "validated": true,
  "summary": "一句话总结",
  "artifacts": [
    {{"filename": "example.pptx", "path": "", "format": "pptx", "role": "primary"}}
  ],
  "stats": {{}}
}}
```
- inspect 任务可返回 `"artifacts": []` 且 `"validated": false`。
- 如果无法完成，请输出简洁失败总结，说明最后一次工具调用的 command、kind、message，不要继续调用工具。

{format_specific_guidance}

## 当前阶段执行说明

{phase_guidance}

## OfficeCLI 参考手册

{skill_content}
"""


async def analyze_node(state: OfficeWorkflowState) -> dict[str, Any]:
    coverage = state.get("coverage", {})
    progress = sum(v for v in coverage.values()) / len(coverage) if coverage else 0.0
    evals = state.get("evaluations", [])
    confidence = evals[-1].get("confidence", 0.0) if evals else 0.0
    return {"progress": progress, "confidence": confidence}


async def preflight_node(state: OfficeWorkflowState) -> dict[str, Any]:
    try:
        configurable = get_config().get("configurable", {}) or {}
    except Exception:
        configurable = {}

    source_files = [str(item).strip() for item in state.get("source_files", []) if str(item).strip()]
    task_profile = dict(state.get("task_profile") or {})
    raw_reference_files = state.get("reference_files", task_profile.get("reference_files", [])) or []
    file_hint = str(state.get("file_hint", "") or "").strip()
    goal = str(state.get("goal", "") or "")
    normalized = normalize_goal_profile(
        goal=goal,
        file_hint=file_hint,
        source_files=source_files,
        reference_files=[str(item) for item in raw_reference_files],
        explicit_format=str(state.get("format_hint", "") or ""),
        explicit_operation=str(state.get("operation_hint", "") or ""),
    )
    format_name = str(normalized.get("format", "") or "")
    operation = str(normalized.get("operation", "") or "")
    runtime_target = infer_office_runtime_target(configurable)
    default_create_file = str(normalized.get("default_create_file", "") or "")
    requested_slide_count = int(normalized.get("requested_slide_count", 0) or 0) or None
    quality_profile = dict(normalized.get("quality_profile") or {})
    build_batch_size = int(normalized.get("build_batch_size", 0) or 0) or 1
    inner_limit = int(normalized.get("inner_recursion_limit", OFFICE_INNER_RECURSION_LIMIT) or OFFICE_INNER_RECURSION_LIMIT)
    reference_files = [str(item).strip() for item in normalized.get("reference_files", []) if str(item).strip()]
    cost_ledger = init_cost_ledger(
        task_id=str(state.get("task_id", "") or "office_domain"),
        domain="office",
        requested_pages=requested_slide_count,
        metadata={
            "operation": operation,
            "format": format_name,
            "runtime_target": runtime_target,
        },
    )
    cost_ledger = append_stage_record(
        cost_ledger,
        stage="planning",
        status="ready",
        elapsed_ms=0,
        metadata={
            "requested_slide_count": requested_slide_count or 0,
            "build_batch_size": build_batch_size,
            "inner_recursion_limit": inner_limit,
            "quality_profile": quality_profile,
        },
    )

    return {
        "format": format_name,
        "operation": operation,
        "file_hint": file_hint or default_create_file,
        "default_create_file": default_create_file,
        "requested_slide_count": requested_slide_count or 0,
        "build_batch_size": build_batch_size,
        "allowed_source_files": source_files,
        "reference_files": reference_files,
        "write_required": is_write_operation(operation),
        "runtime_target_hint": runtime_target,
        "quality_profile": quality_profile,
        "inner_recursion_limit": inner_limit,
        "cost_ledger": cost_ledger,
        "task_profile": {
            "format": format_name,
            "operation": operation,
            "target_filename": default_create_file,
            "file_hint": file_hint or default_create_file,
            "source_files": source_files,
            "reference_files": reference_files,
            "runtime_target": runtime_target,
            "quality_profile": quality_profile,
        },
        "current_stage": "planning",
        "current_batch_index": 0,
        "completed_pages": 0,
        "qa_fix_round": 0,
        "max_qa_fix_rounds": 2,
        "repair_mode": False,
    }


async def select_strategy_node(state: OfficeWorkflowState) -> dict[str, Any]:
    strategy = str(state.get("selected_strategy", "") or "sequential")
    _log.info("Office strategy selected: %s", strategy)

    from agent.platform.emit import safe_emit_progress

    safe_emit_progress(
        "progress.brief",
        {
            "strategy": strategy,
            "text": f"Strategy selected: {strategy}",
            "content": f"Strategy selected: {strategy}",
        },
    )

    return {
        "selected_strategy": strategy,
        "step_history": [{
            "strategy": strategy,
            "confidence": 1.0,
            "reasoning": "Strategy for Office domain",
        }],
    }


async def planning_node(state: OfficeWorkflowState) -> dict[str, Any]:
    requested_slide_count = int(state.get("requested_slide_count", 0) or 0) or 0
    build_batch_size = int(state.get("build_batch_size", 0) or 0) or 1
    default_create_file = str(state.get("default_create_file", "") or "")
    strategy_format = str(state.get("format", "") or state.get("format_hint", "") or "").strip().lower()
    if not strategy_format:
        suffix = Path(default_create_file).suffix.lower().lstrip(".")
        if suffix in {"pptx", "docx", "xlsx"}:
            strategy_format = suffix
        elif any(token in str(state.get("goal", "") or "").lower() for token in ("ppt", "powerpoint", "presentation", "deck", "幻灯片", "演示文稿")):
            strategy_format = "pptx"
    strategy = get_strategy_for_format(strategy_format, operation=str(state.get("operation", "") or ""))
    merged_constraints = resolve_reference_constraints(
        goal_constraints={
            **dict(state.get("goal_constraints") or {}),
            "format": strategy_format,
            "operation": str(state.get("operation", "") or ""),
            "goal": str(state.get("goal", "") or ""),
        },
        reference_structure_constraints={
            **dict(state.get("reference_structure_constraints") or {}),
            "format": strategy_format,
        },
        reference_style_constraints={
            **dict(state.get("reference_style_constraints") or {}),
            "format": strategy_format,
        },
        existing_document_profile={
            **dict(state.get("existing_document_profile") or {}),
            "format": strategy_format,
        },
    )
    raw_plan = strategy.build_plan(
        goal=str(state.get("goal", "") or ""),
        requested_slide_count=requested_slide_count or 6,
        build_batch_size=build_batch_size,
        default_create_file=default_create_file,
        merged_constraints=merged_constraints,
    )
    deck_plan, planner_validation_issues = strategy.validate_plan(
        plan=raw_plan,
        goal=str(state.get("goal", "") or ""),
        requested_slide_count=requested_slide_count or 6,
        build_batch_size=build_batch_size,
        default_create_file=default_create_file,
    )
    if str(state.get("operation", "") or "").lower() == "create":
        refined_filename = refine_filename_from_plan(
            current_filename=default_create_file,
            plan_title=str(deck_plan.get("title", "") or ""),
            format_name=str(strategy_format or ""),
        )
    else:
        refined_filename = default_create_file
    next_task_profile = {
        **dict(state.get("task_profile") or {}),
        "target_filename": refined_filename or default_create_file,
        "merged_constraints": merged_constraints,
    }
    cost_ledger = append_stage_record(
        dict(state.get("cost_ledger") or {}),
        stage="planning",
        status="planned",
        elapsed_ms=0,
        metadata={
            "planned_slide_count": int(deck_plan.get("slide_count", 0) or 0),
            "batch_count": len(deck_plan.get("batches", []) or []),
            "planner_issue_count": len(planner_validation_issues),
        },
    )
    return {
        "deck_plan": deck_plan,
        "planning_summary": {
            "title": str(deck_plan.get("title", "") or ""),
            "slide_count": int(deck_plan.get("slide_count", 0) or 0),
            "batch_count": len(deck_plan.get("batches", []) or []),
        },
        "planner_validation_issues": planner_validation_issues,
        "task_profile": next_task_profile,
        "current_stage": "build",
        "current_batch_index": 0,
        "cost_ledger": cost_ledger,
        "default_create_file": refined_filename,
        "file_hint": refined_filename or str(state.get("file_hint", "") or ""),
    }


async def build_node(state: OfficeWorkflowState) -> dict[str, Any]:
    format_hint = str(state.get("format", "") or state.get("format_hint", "") or "auto")
    operation = str(state.get("operation", "") or "create")
    requested_slide_count = int(state.get("requested_slide_count", 0) or 0) or None
    strategy = get_strategy_for_format(format_hint if format_hint != "auto" else "", operation=operation)
    format_specific_guidance = _build_format_specific_guidance(
        goal=str(state.get("goal", "") or ""),
        format_name=format_hint if format_hint != "auto" else "",
        operation=operation,
        requested_slide_count=requested_slide_count,
    )
    return await run_section_builder(
        state,
        strategy=strategy,
        system_template=_OFFICE_SYSTEM,
        format_specific_guidance=format_specific_guidance,
        office_model_role=OFFICE_MODEL_ROLE,
        subagents=_build_subagent_dicts(),
    )


async def qa_fix_node(state: OfficeWorkflowState) -> dict[str, Any]:
    format_name = str(state.get("format", "") or state.get("format_hint", "") or "")
    operation = str(state.get("operation", "") or "")
    strategy = get_strategy_for_format(format_name, operation=operation)
    return run_quality_gate(state, strategy=strategy)


# Backward-compatible aliases for existing tests and call sites.
exec_sequential = build_node
evaluate_node = qa_fix_node


def build_office_workflow_graph() -> Any:
    graph = StateGraph(OfficeWorkflowState)
    graph.add_node("analyze", analyze_node)
    graph.add_node("preflight", preflight_node)
    graph.add_node("select_strategy", select_strategy_node)
    graph.add_node("planning", planning_node)
    graph.add_node("build", build_node)
    graph.add_node("qa_fix", qa_fix_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "preflight")
    graph.add_edge("preflight", "select_strategy")
    graph.add_edge("select_strategy", "planning")
    graph.add_edge("planning", "build")
    graph.add_conditional_edges(
        "build",
        route_after_build,
        {"build": "build", "qa_fix": "qa_fix", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "qa_fix",
        route_after_qa_fix,
        {"build": "build", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)
    return graph.compile(name="office_workflow")
