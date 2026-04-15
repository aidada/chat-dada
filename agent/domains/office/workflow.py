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

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphRecursionError
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from typing_extensions import TypedDict

from core.content_utils import extract_result_text
from core.models import build_chat_model
from deepagents import create_deep_agent
from langgraph.config import get_config
from agent.domains.office.result_utils import (
    coerce_office_operation,
    extract_office_result_json,
    is_write_operation,
)
from agent.domains.office.tools import get_office_tools
from agent.domains.research.tools import get_research_tools
from agent.hands.deepagents_backend import resolve_deepagents_runtime
from agent.platform.streaming import stream_nested_graph
from agent.tools.officecli import ALLOWED_DIR, infer_office_runtime_target
from agent.tools.officecli_skill_loader import build_officecli_skill_bundle

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
)
_FORMAT_DEFAULT_FILENAMES = {
    "pptx": "presentation",
    "docx": "document",
    "xlsx": "workbook",
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


class OfficeWorkflowState(TypedDict, total=False):
    goal: str
    task_id: str
    report_profile: str
    format_hint: str
    file_hint: str
    default_create_file: str
    requested_slide_count: int
    source_files: list[str]
    operation_hint: str

    format: str
    operation: str
    allowed_source_files: list[str]
    write_required: bool
    runtime_target_hint: str

    selected_strategy: str
    step_history: Annotated[list[dict[str, Any]], "add"]

    progress: float
    confidence: float
    coverage: dict[str, bool]
    cost: float
    max_cost: float
    max_steps: int
    inner_recursion_limit: int

    intermediate_results: Annotated[list[dict[str, Any]], "add"]
    evaluations: Annotated[list[dict[str, Any]], "add"]
    final_result: str
    terminal_status: str
    terminal_reason: str


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


def _extract_explicit_filename(text: str, format_name: str) -> str | None:
    raw = str(text or "").strip()
    if not raw:
        return None

    suffix = Path(raw).suffix.lower().lstrip(".")
    if suffix in {"pptx", "docx", "xlsx"}:
        candidate = Path(raw).name
        if format_name and suffix != format_name:
            return None
        return candidate

    match = _EXPLICIT_FILENAME_RE.search(raw)
    if not match:
        return None

    candidate = Path(match.group(1)).name
    suffix = Path(candidate).suffix.lower().lstrip(".")
    if format_name and suffix != format_name:
        return None
    return candidate


def _infer_default_create_file(goal: str, file_hint: str, format_name: str) -> str:
    if not format_name:
        return ""

    explicit = _extract_explicit_filename(file_hint, format_name) or _extract_explicit_filename(goal, format_name)
    if explicit:
        return explicit

    lowered = str(goal or "").lower()
    stem_parts: list[str] = []

    for token in _ASCII_FILENAME_TOKEN_RE.findall(lowered):
        normalized = token.strip("-").lower()
        if (
            not normalized
            or normalized in _FILENAME_STOPWORDS
            or normalized.isdigit()
            or normalized.endswith((".pptx", ".docx", ".xlsx"))
        ):
            continue
        if normalized not in stem_parts:
            stem_parts.append(normalized)
        if len(stem_parts) >= 2:
            break

    for keywords, label in _INTENT_FILENAME_HINTS:
        if any(keyword in lowered for keyword in keywords) and label not in stem_parts:
            stem_parts.append(label)
        if len(stem_parts) >= 3:
            break

    if not stem_parts:
        stem_parts.append(_FORMAT_DEFAULT_FILENAMES.get(format_name, "office-file"))

    stem = "-".join(stem_parts[:3])
    stem = re.sub(r"[^a-z0-9_-]+", "-", stem).strip("-_")
    if not stem:
        stem = _FORMAT_DEFAULT_FILENAMES.get(format_name, "office-file")
    stem = stem[:64].rstrip("-_") or _FORMAT_DEFAULT_FILENAMES.get(format_name, "office-file")
    return f"{stem}.{format_name}"


def _infer_requested_slide_count(goal: str) -> int | None:
    match = _REQUESTED_SLIDE_COUNT_RE.search(str(goal or ""))
    if not match:
        return None
    try:
        count = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return count if 1 <= count <= 30 else None


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
    file_hint = str(state.get("file_hint", "") or "").strip()
    goal = str(state.get("goal", "") or "")
    format_name = _infer_format(goal, file_hint, source_files, str(state.get("format_hint", "") or ""))
    operation = _infer_operation(goal, source_files, str(state.get("operation_hint", "") or ""))
    runtime_target = infer_office_runtime_target(configurable)
    default_create_file = _infer_default_create_file(goal, file_hint, format_name) if operation == "create" else ""
    requested_slide_count = _infer_requested_slide_count(goal) if format_name == "pptx" else None

    return {
        "format": format_name,
        "operation": operation,
        "file_hint": file_hint or default_create_file,
        "default_create_file": default_create_file,
        "requested_slide_count": requested_slide_count or 0,
        "allowed_source_files": source_files,
        "write_required": is_write_operation(operation),
        "runtime_target_hint": runtime_target,
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


async def exec_sequential(state: OfficeWorkflowState) -> dict[str, Any]:
    _safe_emit("step", "Office: Sequential execution...")

    context_parts = [
        r["output"]
        for r in state.get("intermediate_results", [])
        if r.get("output")
    ]
    context = "\n\n---\n\n".join(context_parts[-3:]) if context_parts else ""
    latest_evaluation = (state.get("evaluations") or [])[-1] if state.get("evaluations") else {}
    latest_issues = latest_evaluation.get("issues", []) if isinstance(latest_evaluation, dict) else []
    qa_feedback = "\n".join(
        f"- {str(issue.get('message', '') or '').strip()}"
        for issue in latest_issues
        if str(issue.get("message", "") or "").strip()
    )
    source_files = list(state.get("allowed_source_files", []) or [])
    source_lines = "\n".join(f"- {item}" for item in source_files) if source_files else "- 无"
    format_hint = str(state.get("format", "") or state.get("format_hint", "") or "auto")
    operation = str(state.get("operation", "") or "create")
    runtime_target = str(state.get("runtime_target_hint", "") or "server")
    default_create_file = str(state.get("default_create_file", "") or "")
    requested_slide_count = int(state.get("requested_slide_count", 0) or 0) or None

    skill_content = build_officecli_skill_bundle(
        state["goal"],
        file_hint=str(state.get("file_hint", "") or default_create_file),
        format_hint=format_hint if format_hint != "auto" else None,
        operation_hint=operation,
    )
    format_specific_guidance = _build_format_specific_guidance(
        goal=str(state.get("goal", "") or ""),
        format_name=format_hint if format_hint != "auto" else "",
        operation=operation,
        requested_slide_count=requested_slide_count,
    )
    system_prompt = _OFFICE_SYSTEM.format(
        format_hint=format_hint,
        operation=operation,
        runtime_target=runtime_target,
        default_create_file=default_create_file or "-",
        source_files_block=source_lines,
        format_specific_guidance=format_specific_guidance,
        skill_content=skill_content,
    )

    try:
        configurable = get_config().get("configurable", {}) or {}
    except Exception:
        configurable = {}
    task_id = str(state.get("task_id", "") or configurable.get("thread_id", "") or "office_domain")
    tools, backend = resolve_deepagents_runtime(
        domain="office",
        task_id=task_id,
        fallback_tools=list(get_office_tools()),
        configurable=configurable,
    )

    agent = create_deep_agent(
        model=build_chat_model(OFFICE_MODEL_ROLE),
        system_prompt=system_prompt,
        tools=tools,
        subagents=_build_subagent_dicts(),
        backend=backend,
        checkpointer=False,
        name="office_sequential",
    )

    input_sections = [
        state["goal"],
        "",
        "执行上下文：",
        f"- operation: {operation}",
        f"- format: {format_hint}",
        f"- runtime_target: {runtime_target}",
    ]
    if default_create_file:
        input_sections.append(f"- default_create_file: {default_create_file}")
    if requested_slide_count is not None:
        input_sections.append(f"- requested_slide_count: {requested_slide_count}")
    if source_files:
        input_sections.append("- source_files:")
        input_sections.extend(f"  - {item}" for item in source_files)
    if context:
        input_sections.extend(["", "已有上下文：", context])
    if qa_feedback:
        input_sections.extend(["", "上轮 QA 未通过，必须先修正这些问题：", qa_feedback])
    input_msg = "\n".join(input_sections)

    office_constraints = {
        "allowed_source_files": source_files,
        "allowed_output_dir": str(ALLOWED_DIR),
        "runtime_target": runtime_target,
        "default_create_file": default_create_file,
    }
    inner_limit = int(
        state.get("inner_recursion_limit", OFFICE_INNER_RECURSION_LIMIT)
        or OFFICE_INNER_RECURSION_LIMIT
    )
    try:
        response = await stream_nested_graph(
            agent,
            {"messages": [HumanMessage(content=input_msg)]},
            config={
                "recursion_limit": inner_limit,
                "configurable": {
                    "nested_recursion_limit": inner_limit,
                    "office_constraints": office_constraints,
                },
            },
            extra_payload={
                "nested_graph": "office_sequential",
                "strategy": "sequential",
                "source": "office_workflow",
            },
        )
        output = _extract_last_ai_text(response)
    except GraphRecursionError:
        output = (
            f"Office 任务已中止：内层 agent 超过 {inner_limit} 步仍未收敛，"
            "疑似重复工具调用。请检查 officecli 返回或提示词收敛规则。"
        )
        _safe_emit("step", output)
        return {
            "intermediate_results": [{
                "strategy": "sequential",
                "output": output,
                "bounded_failure": True,
                "reason": "inner_recursion_limit",
            }],
            "evaluations": [{
                "passed": False,
                "confidence": 0.0,
                "issues": [{
                    "severity": "error",
                    "message": "Office inner agent hit recursion limit",
                    "metadata": {"limit": inner_limit},
                }],
            }],
            "final_result": output,
            "confidence": 0.0,
            "terminal_status": "bounded_failure",
            "terminal_reason": "inner_recursion_limit",
        }
    except Exception as exc:
        output = f"Office 任务失败：内层 agent 执行异常：{exc}"
        _log.exception("Office sequential agent failed: %s", exc)
        _safe_emit("step", output)
        return {
            "intermediate_results": [{
                "strategy": "sequential",
                "output": output,
                "bounded_failure": True,
                "reason": "inner_agent_exception",
            }],
            "evaluations": [{
                "passed": False,
                "confidence": 0.0,
                "issues": [{
                    "severity": "error",
                    "message": "Office inner agent raised an exception",
                    "metadata": {"error": str(exc)},
                }],
            }],
            "final_result": output,
            "confidence": 0.0,
            "terminal_status": "error",
            "terminal_reason": "inner_agent_exception",
        }

    _safe_emit("step", f"Office: Sequential done ({len(output)} chars)")
    return {
        "intermediate_results": [{"strategy": "sequential", "output": output}],
    }


async def evaluate_node(state: OfficeWorkflowState) -> dict[str, Any]:
    terminal_status = str(state.get("terminal_status", "") or "")
    if terminal_status:
        evaluation = {
            "passed": False,
            "confidence": 0.0,
            "issues": [{
                "severity": "error",
                "message": str(state.get("terminal_reason", terminal_status)),
                "metadata": {"terminal_status": terminal_status},
            }],
        }
        return {
            "evaluations": state.get("evaluations") or [evaluation],
            "final_result": str(state.get("final_result", "") or ""),
            "confidence": float(state.get("confidence", 0.0) or 0.0),
        }

    results = state.get("intermediate_results", [])
    if not results:
        return {
            "evaluations": [{
                "passed": False,
                "confidence": 0.0,
                "issues": [{"severity": "error", "message": "策略未产出任何输出"}],
            }],
        }

    output = str(results[-1].get("output", "") or "")
    if not output:
        return {
            "evaluations": [{
                "passed": False,
                "confidence": 0.0,
                "issues": [{"severity": "error", "message": "策略未产出任何输出"}],
            }],
        }

    meta = extract_office_result_json(output)
    if meta is None:
        return {
            "evaluations": [{
                "passed": False,
                "confidence": 0.0,
                "issues": [{
                    "severity": "error",
                    "message": "最终回复缺少结构化 Office JSON 结果",
                }],
            }],
        }

    operation = coerce_office_operation(meta.get("operation") or state.get("operation"))
    validated = bool(meta.get("validated", False))
    artifacts = meta.get("artifacts") if isinstance(meta.get("artifacts"), list) else []
    summary = str(meta.get("summary", "") or "").strip()
    stats = meta.get("stats") if isinstance(meta.get("stats"), dict) else {}
    issues: list[dict[str, Any]] = []

    if operation != "inspect" and not artifacts:
        issues.append({"severity": "error", "message": "写入型 Office 任务缺少 artifacts"})
    if bool(state.get("write_required")) and not validated:
        issues.append({"severity": "error", "message": "写入型 Office 任务未完成 validate"})
    if operation == "inspect" and not summary and not output.strip():
        issues.append({"severity": "error", "message": "inspect 任务缺少有效总结"})
    issues.extend(
        _evaluate_ppt_quality_stats(
            format_name=str(state.get("format", "") or state.get("format_hint", "") or ""),
            operation=operation,
            stats=stats,
        )
    )

    passed = not any(issue["severity"] == "error" for issue in issues)
    evaluation = {
        "passed": passed,
        "confidence": 0.9 if passed else 0.0,
        "issues": issues,
    }

    if passed:
        _safe_emit("step", "Office review passed")
        return {
            "evaluations": [evaluation],
            "final_result": output,
            "confidence": 0.9,
        }

    _safe_emit("step", f"Office review failed ({len(issues)} issues)")
    return {
        "evaluations": [evaluation],
        "confidence": 0.0,
    }


def _evaluate_ppt_quality_stats(
    *,
    format_name: str,
    operation: str,
    stats: dict[str, Any],
) -> list[dict[str, Any]]:
    if str(format_name).lower() != "pptx" or operation not in {"create", "transform"}:
        return []

    required_int_fields = (
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
    )
    issues: list[dict[str, Any]] = []
    if not isinstance(stats, dict) or not stats:
        return [{"severity": "error", "message": "PPT 创建结果缺少质量 stats"}]

    normalized: dict[str, int] = {}
    missing_fields: list[str] = []
    for key in required_int_fields:
        value = stats.get(key)
        if isinstance(value, bool) or value is None:
            missing_fields.append(key)
            continue
        try:
            normalized[key] = int(value)
        except (TypeError, ValueError):
            missing_fields.append(key)
    if missing_fields:
        issues.append(
            {
                "severity": "error",
                "message": f"PPT 质量 stats 缺少或非法字段: {', '.join(missing_fields)}",
            }
        )
        return issues

    qa_checks = stats.get("qa_checks")
    qa_values = {str(item).strip() for item in qa_checks} if isinstance(qa_checks, list) else set()
    required_checks = {"view_stats", "view_annotated", "validate"}
    if not required_checks.issubset(qa_values):
        issues.append(
            {
                "severity": "error",
                "message": "PPT QA 未完整执行：必须包含 view_stats、view_annotated、validate",
            }
        )

    slide_count = normalized["slide_count"]
    content_slide_count = normalized["content_slide_count"]
    notes_slide_count = normalized["notes_slide_count"]
    transition_slide_count = normalized["transition_slide_count"]
    visual_slide_count = normalized["visual_slide_count"]
    text_only_slide_count = normalized["text_only_slide_count"]
    layout_variety_count = normalized["layout_variety_count"]

    if slide_count <= 0:
        issues.append({"severity": "error", "message": "PPT slide_count 必须大于 0"})
        return issues
    if content_slide_count < 0 or content_slide_count > slide_count:
        issues.append({"severity": "error", "message": "PPT content_slide_count 不合法"})
    if notes_slide_count < content_slide_count:
        issues.append({"severity": "error", "message": "并非所有内容 slide 都有 speaker notes"})
    if slide_count > 1 and transition_slide_count < slide_count - 1:
        issues.append({"severity": "error", "message": "PPT 第 2 张及之后的 slide 缺少 transition"})
    if visual_slide_count < max(1, content_slide_count):
        issues.append({"severity": "error", "message": "PPT 视觉密度不足：内容 slide 缺少非文字视觉元素"})
    if text_only_slide_count > 0:
        issues.append({"severity": "error", "message": "PPT 仍存在 text-only slides"})
    if slide_count >= 3 and layout_variety_count < min(3, slide_count):
        issues.append({"severity": "error", "message": "PPT 布局变化不足，缺少版式多样性"})

    return issues


def route_to_strategy(state: OfficeWorkflowState) -> str:
    return f"exec_{state['selected_strategy']}"


def should_continue(state: OfficeWorkflowState) -> str:
    if state.get("final_result"):
        return "done"
    if state.get("terminal_status"):
        return "done"

    max_cost = state.get("max_cost", OFFICE_MAX_COST)
    if state.get("cost", 0.0) >= max_cost:
        _log.warning("Office cost limit reached: $%.2f", state["cost"])
        return "done"

    max_steps = state.get("max_steps", OFFICE_MAX_STEPS)
    if len(state.get("step_history", [])) >= max_steps:
        _log.warning("Office step limit reached: %d", len(state["step_history"]))
        return "done"
    return "continue"


def build_office_workflow_graph() -> Any:
    graph = StateGraph(OfficeWorkflowState)
    graph.add_node("analyze", analyze_node)
    graph.add_node("preflight", preflight_node)
    graph.add_node("select_strategy", select_strategy_node)
    graph.add_node("exec_sequential", exec_sequential)
    graph.add_node("evaluate", evaluate_node)

    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "preflight")
    graph.add_edge("preflight", "select_strategy")
    graph.add_conditional_edges(
        "select_strategy",
        route_to_strategy,
        {"exec_sequential": "exec_sequential"},
    )
    graph.add_edge("exec_sequential", "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        should_continue,
        {"continue": "analyze", "done": END},
    )
    return graph.compile(name="office_workflow")
