"""
PPT domain internal workflow.

Provides domain-specific LangGraph workflow for PPT tasks.
Uses sequential strategy as defined in PRD §8.3 C1.

This module inlines the necessary orchestrator logic to remove
dependency on agent.workflows.orchestrator.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
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
from agent.capabilities.review_gates import ReviewGate
from agent.domains.ppt.tools import get_ppt_tools
from agent.domains.research.tools import get_research_tools
from agent.hands.deepagents_backend import resolve_deepagents_runtime
from agent.platform.streaming import stream_nested_graph
from agent.tools.officecli_skill_loader import build_officecli_skill_bundle

_log = logging.getLogger("chatdada.ppt.workflow")

# ── Domain Configuration (PRD §8.3 C3/C1) ──────────────────────────────────────

PPT_MODEL_ROLE = "orchestrator"
PPT_MAX_STEPS = 15  # More steps needed: agent iterates create→add→validate→fix
PPT_MAX_COST = 3.0
# LangGraph recursion counts internal agent/tool graph transitions, not just visible tool turns.
# A normal PPT create -> populate -> validate -> fix flow can consume far more than a dozen
# recursion steps, so keep the guardrail bounded but high enough for legitimate runs.
PPT_INNER_RECURSION_LIMIT = 40

# ── SubagentConfig (local definition, PRD §8.3 C3) ──────────────────────────────

@dataclass
class SubagentConfig:
    """Configuration for a deepagents subagent."""
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


# ── PPT subagents ──────────────────────────────────────────────────────────────

_CONTENT_RESEARCHER_TOOL_NAMES = {
    "exa_deep_search",
    "academic_search",
    "web_search",
    "brave_search",
    "browser_navigate",
}


def _build_content_researcher_tools() -> list[Any]:
    """Return real search/browser tools for PPT content research."""
    return [
        tool
        for tool in get_research_tools()
        if getattr(tool, "name", "") in _CONTENT_RESEARCHER_TOOL_NAMES
    ]

PPT_SUBAGENTS = [
    SubagentConfig(
        name="content_researcher",
        description="Search for relevant data, statistics, and materials for PPT slides.",
        system_prompt="搜索与 PPT 主题相关的数据、案例和素材。输出结构化的要点和来源。",
        tools=_build_content_researcher_tools(),
    ),
]

# ── Workflow State ─────────────────────────────────────────────────────────────

class PptWorkflowState(TypedDict, total=False):
    """State for PPT domain workflow."""
    # Input
    goal: str
    task_id: str
    report_profile: str

    # Strategy control
    selected_strategy: str
    step_history: Annotated[list[dict[str, Any]], "add"]

    # Progress signals
    progress: float
    confidence: float
    coverage: dict[str, bool]
    cost: float
    max_cost: float
    max_steps: int
    inner_recursion_limit: int

    # Results
    intermediate_results: Annotated[list[dict[str, Any]], "add"]
    evaluations: Annotated[list[dict[str, Any]], "add"]
    final_result: str
    terminal_status: str
    terminal_reason: str


# ── Helper functions ───────────────────────────────────────────────────────────

from agent.platform.emit import safe_emit_progress_with_content as _safe_emit


def _extract_last_ai_text(response: Any) -> str:
    """Extract text from the last AIMessage in a deepagents response."""
    messages = response.get("messages", []) if isinstance(response, dict) else []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = extract_result_text(getattr(msg, "content", ""))
            if text:
                return text
    return ""


def _build_subagent_dicts() -> list[dict[str, Any]]:
    return [s.to_dict() for s in PPT_SUBAGENTS]


def _load_officecli_skill() -> str:
    """Backward-compatible helper kept for older tests."""
    return build_officecli_skill_bundle("", format_hint="pptx")


# ── System prompt ──────────────────────────────────────────────────────────────

_PPT_SYSTEM = """\
你是 PPT 生成专家。你可以用结构化 officecli / officecli_batch 工具直接创建和编辑 PowerPoint 文件。

## 工作流程

1. **搜索素材**：先用 web_search / academic_search 等工具搜索相关素材和数据
2. **规划大纲**：确定 PPT 结构（封面、各章节、总结）
3. **创建 PPT**：用 `officecli(verb="create", file="<filename>.pptx")` 创建空文件
4. **逐步构建**：用 `officecli` 或 `officecli_batch` 调用官方 OfficeCLI verb（如 `add` / `set` / `view`）
5. **验证检查**：用 `officecli(verb="validate", file="<filename>.pptx")` 检查文件质量
6. **修复问题**：如果 validate 或 view issues 发现问题，用 set/remove 修复

## 关键规则

- 文件名只用英文字母/数字/下划线，例如 "report_q4.pptx"
- 所有文件操作自动在 outputs/ 目录下进行，只传文件名
- 不确定属性名时，先运行 `officecli(verb="help", format="pptx", options={{"topic": ["set", "shape"]}})` 查询帮助
- 每页正文控制在 50-80 字，要点化表达
- 使用中文内容
- 完成后必须运行 validate 确认文件有效
- 禁止编写或执行 Python、bash、shell 脚本来生成 PPT；只能调用 OfficeCLI 工具
- 不要猜测伪命令如 `add slide`、`set title`、`pptx add_slide`
- OfficeCLI 按官方 CLI 语义工作：优先使用 `create/add/set/view/get/query/remove/validate/help`
- `officecli` / `officecli_batch` 返回的是 JSON 字符串，重点看 `success`、`kind`、`message`
- 一旦收到 `kind=validation_passed`，禁止继续调用任何工具，立即输出最终 JSON
- 一旦收到 `kind=fatal_error`，停止继续修复，直接输出失败总结
- 如果相同 `command + kind + message` 连续出现 2 次，停止重试并输出失败总结
- `kind=help` 只用于理解命令，不要反复查询同一帮助
- `kind=validation_failed` 说明文件仍有问题，只能做有限修复；不要无限尝试

## 输出要求

完成 PPT 创建后，你的最终回复必须包含以下 JSON（用 ```json 包裹）：
```json
{{"filename": "<文件名>.pptx", "title": "<PPT标题>", "slide_count": <页数>}}
```

如果无法完成，请输出简洁失败总结，说明最后一次工具调用的 `command`、`kind`、`message`，不要继续调用工具。

## OfficeCLI 参考手册

{skill_content}
"""


# ── Strategy nodes ─────────────────────────────────────────────────────────────

async def exec_sequential(state: PptWorkflowState) -> dict[str, Any]:
    """Sequential strategy: single deepagents instance."""
    _safe_emit("step", "PPT: Sequential execution...")

    # Build context from prior results
    context_parts = [
        r["output"]
        for r in state.get("intermediate_results", [])
        if r.get("output")
    ]
    context = "\n\n---\n\n".join(context_parts[-3:]) if context_parts else ""

    skill_content = build_officecli_skill_bundle(
        state["goal"],
        format_hint="pptx",
        operation_hint="create",
    )
    system_prompt = _PPT_SYSTEM.format(skill_content=skill_content)
    try:
        configurable = get_config().get("configurable", {}) or {}
    except Exception:
        configurable = {}
    task_id = str(state.get("task_id", "") or configurable.get("thread_id", "") or "ppt_domain")
    tools, backend = resolve_deepagents_runtime(
        domain="ppt",
        task_id=task_id,
        fallback_tools=list(get_ppt_tools()),
        configurable=configurable,
    )

    agent = create_deep_agent(
        model=build_chat_model(PPT_MODEL_ROLE),
        system_prompt=system_prompt,
        tools=tools,
        subagents=_build_subagent_dicts(),
        backend=backend,
        checkpointer=False,
        name="ppt_sequential",
    )

    input_msg = (
        f"{state['goal']}\n\n已有上下文：\n{context}"
        if context
        else state["goal"]
    )
    inner_limit = int(
        state.get("inner_recursion_limit", PPT_INNER_RECURSION_LIMIT)
        or PPT_INNER_RECURSION_LIMIT
    )
    try:
        response = await stream_nested_graph(
            agent,
            {"messages": [HumanMessage(content=input_msg)]},
            config={
                "recursion_limit": inner_limit,
                "configurable": {
                    "nested_recursion_limit": inner_limit,
                },
            },
            extra_payload={
                "nested_graph": "ppt_sequential",
                "strategy": "sequential",
                "source": "ppt_workflow",
            },
        )
        output = _extract_last_ai_text(response)
    except GraphRecursionError:
        output = (
            f"PPT 生成已中止：内层 agent 超过 {inner_limit} 步仍未收敛，"
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
                    "message": "PPT inner agent hit recursion limit",
                    "metadata": {"limit": inner_limit},
                }],
            }],
            "final_result": output,
            "confidence": 0.0,
            "terminal_status": "bounded_failure",
            "terminal_reason": "inner_recursion_limit",
        }
    except Exception as exc:
        output = f"PPT 生成失败：内层 agent 执行异常：{exc}"
        _log.exception("PPT sequential agent failed: %s", exc)
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
                    "message": "PPT inner agent raised an exception",
                    "metadata": {"error": str(exc)},
                }],
            }],
            "final_result": output,
            "confidence": 0.0,
            "terminal_status": "error",
            "terminal_reason": "inner_agent_exception",
        }

    _safe_emit("step", f"PPT: Sequential done ({len(output)} chars)")
    return {
        "intermediate_results": [{"strategy": "sequential", "output": output}],
    }


# ── Analyze node ───────────────────────────────────────────────────────────────

async def analyze_node(state: PptWorkflowState) -> dict[str, Any]:
    """Compute progress signals from current state."""
    coverage = state.get("coverage", {})
    if coverage:
        progress = sum(v for v in coverage.values()) / len(coverage)
    else:
        progress = 0.0

    evals = state.get("evaluations", [])
    confidence = evals[-1].get("confidence", 0.0) if evals else 0.0

    return {
        "progress": progress,
        "confidence": confidence,
    }


# ── Strategy selection ─────────────────────────────────────────────────────────

async def select_strategy_node(state: PptWorkflowState) -> dict[str, Any]:
    """Select execution strategy.

    PPT domain prefers: sequential only.
    """
    if state.get("selected_strategy"):
        strategy = state["selected_strategy"]
    else:
        strategy = "sequential"

    _log.info("PPT strategy selected: %s", strategy)

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
            "reasoning": f"Strategy for PPT domain",
        }],
    }


# ── Evaluator node ─────────────────────────────────────────────────────────────

async def evaluate_node(state: PptWorkflowState) -> dict[str, Any]:
    """Evaluate using basic ReviewGate."""
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
            "evaluations": [
                {"passed": False, "confidence": 0.0, "issues": []},
            ],
        }

    last = results[-1]
    output = last.get("output", "")

    if not output:
        return {
            "evaluations": [
                {
                    "passed": False,
                    "confidence": 0.0,
                    "issues": [
                        {"severity": "error", "message": "策略未产出任何输出"},
                    ],
                },
            ],
        }

    # Run basic ReviewGate
    evaluator = ReviewGate()
    review = await evaluator.evaluate({"report": output})

    evaluation: dict[str, Any] = {
        "passed": review.passed,
        "confidence": 0.9 if review.passed else 0.4,
        "issues": [
            {
                "severity": i.severity,
                "message": i.message,
                "metadata": i.metadata,
            }
            for i in review.issues
        ],
    }

    if review.passed:
        _safe_emit("step", "PPT review passed")
        return {
            "evaluations": [evaluation],
            "final_result": output,
            "confidence": 0.9,
        }

    issue_count = len(review.issues)
    _safe_emit(
        "step",
        f"PPT review failed ({issue_count} issues)",
    )
    return {
        "evaluations": [evaluation],
        "confidence": 0.4,
    }


# ── Control flow ───────────────────────────────────────────────────────────────

def route_to_strategy(state: PptWorkflowState) -> str:
    return f"exec_{state['selected_strategy']}"


def should_continue(state: PptWorkflowState) -> str:
    if state.get("final_result"):
        return "done"

    if state.get("terminal_status"):
        return "done"

    max_cost = state.get("max_cost", PPT_MAX_COST)
    if state.get("cost", 0) >= max_cost:
        _log.warning("PPT cost limit reached: $%.2f", state["cost"])
        return "done"

    max_steps = state.get("max_steps", PPT_MAX_STEPS)
    if len(state.get("step_history", [])) >= max_steps:
        _log.warning("PPT step limit reached: %d", len(state["step_history"]))
        return "done"

    return "continue"


# ── Graph builder ──────────────────────────────────────────────────────────────

def build_ppt_workflow_graph() -> Any:
    """Build PPT domain internal LangGraph.

    Workflow:
        START → analyze → select_strategy → exec_sequential → evaluate
                  ↑                                              │
                  └──────────── continue ────────────────────────┘
                                                                 │
                                                              done → END
    """
    graph = StateGraph(PptWorkflowState)

    # Nodes
    graph.add_node("analyze", analyze_node)
    graph.add_node("select_strategy", select_strategy_node)
    graph.add_node("exec_sequential", exec_sequential)
    graph.add_node("evaluate", evaluate_node)

    # Edges
    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "select_strategy")
    graph.add_conditional_edges(
        "select_strategy",
        route_to_strategy,
        {
            "exec_sequential": "exec_sequential",
        },
    )
    graph.add_edge("exec_sequential", "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        should_continue,
        {"continue": "analyze", "done": END},
    )

    return graph.compile(name="ppt_workflow")
