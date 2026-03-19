"""
Deep Research Agent — LangGraph graph definitions.
"""
import logging
import time
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from capabilities.context_manager import ResearchContext
from capabilities.memory import ResearchMemory
from capabilities.planner import ResearchPlan, generate_research_plan, get_next_subtask, is_plan_complete
from capabilities.progress_tracker import ProgressTracker, extract_gaps_from_summary, extract_progress_from_tool_results
from core.content_utils import extract_text_content, normalize_markdown_report
from core.models import get_llm

from agents.deep_research.config import DEFAULT_REPORT_PROFILE, ResearchConfig, ResearchState
from agents.deep_research.prompts import _build_research_messages, _looks_like_academic_paper_task
from agents.deep_research.utils import _generate_structured_summary, _latest_tool_messages, _synthesize_parallel_findings
from agents.deep_research.run import CORE_TOOLS

from tools.research_notes import set_research_context

log = logging.getLogger("chatdada.agent")

_SEARCH_TOOL_NAMES = {"web_search", "brave_search", "academic_search", "exa_deep_search"}
_ACADEMIC_GAP_KEYWORDS = ("论文", "paper", "实验", "experiment", "数据", "data", "baseline", "ablation")


def _select_search_tools(state: dict, all_search_tools: list) -> list:
    """Dynamically filter search tools based on research stage, query features, and progress."""
    step = state.get("step_count", 0)
    query = state.get("query", "")
    progress = state.get("progress", {})
    is_academic = _looks_like_academic_paper_task(query)

    available = {"web_search", "brave_search"}

    if is_academic:
        available.add("academic_search")

    if step >= 4:
        available.add("exa_deep_search")

    gaps = progress.get("gaps", []) or progress.get("remaining_gaps", [])
    findings = progress.get("findings", []) or progress.get("key_findings_so_far", [])
    gap_text = " ".join(gaps).lower() if gaps else ""

    if any(kw in gap_text for kw in _ACADEMIC_GAP_KEYWORDS):
        available.add("academic_search")
        if step >= 2:
            available.add("exa_deep_search")

    if len(findings) >= 8:
        available.discard("brave_search")

    return [t for t in all_search_tools if t.name in available]


def _apply_tool_selection(state: dict, all_tools: list) -> list:
    """Split tools into search/non-search, filter search tools, then recombine."""
    search_tools = [t for t in all_tools if t.name in _SEARCH_TOOL_NAMES]
    non_search_tools = [t for t in all_tools if t.name not in _SEARCH_TOOL_NAMES]
    selected_search = _select_search_tools(state, search_tools)
    return non_search_tools + selected_search


async def research_planner(state: ResearchState) -> dict:
    llm = get_llm("deep_research").bind_tools(CORE_TOOLS)

    # --- progress tracking ---
    tracker = ProgressTracker.from_dict(state.get("progress", {})) if state.get("progress") else ProgressTracker(original_query=state["query"])
    tool_msgs = _latest_tool_messages(state["messages"])
    step = state["step_count"]

    # Find previous AI message for extracting tool call queries
    prev_ai = None
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage):
            prev_ai = msg
            break

    completed_qs, findings_extracted, failed_qs = extract_progress_from_tool_results(tool_msgs, prev_ai)
    for q in completed_qs:
        tracker.record_search(q, success=True)
    for q in failed_qs:
        tracker.record_search(q, success=False)
    for f in findings_extracted:
        tracker.record_finding(f)
    attention_block = tracker.build_attention_block()

    # --- three-tier context management ---
    ctx = ResearchContext.from_dict(state.get("research_context", {})) if state.get("research_context") else ResearchContext()
    ctx.merge_tool_results(tool_msgs, step)
    await ctx.trigger_compression(step)
    prompt_context = ctx.build_prompt_context()

    messages = _build_research_messages(
        state["query"],
        prompt_context,
        state.get("report_profile", DEFAULT_REPORT_PROFILE),
        attention_block=attention_block,
    )
    response = await llm.ainvoke(messages)
    return {
        "messages": [response],
        "step_count": step + 1,
        "research_context": ctx.to_dict(),
        "progress": tracker.to_dict(),
    }


async def research_tools(state: ResearchState) -> dict:
    return await ToolNode(CORE_TOOLS).ainvoke(state)


def research_finish(state: ResearchState) -> dict:
    # Try to find a textual AIMessage (LLM's final answer)
    for msg in reversed(state["messages"]):
        if not isinstance(msg, AIMessage):
            continue
        text = normalize_markdown_report(extract_text_content(msg))
        if text:
            return {"research_context": state.get("research_context", {}), "_final_text": text}

    # Fallback: build from ResearchContext
    ctx = ResearchContext.from_dict(state.get("research_context", {}))
    fallback = ctx.build_final_context()
    if fallback:
        log.warning("research_finish: no textual AIMessage, using ResearchContext fallback")
    else:
        log.warning("research_finish: no content found")
    return {"research_context": state.get("research_context", {}), "_final_text": normalize_markdown_report(fallback)}


def research_should_continue(state: ResearchState) -> Literal["tools", "finish"]:
    if state["step_count"] >= 15:
        return "finish"
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "finish"


def build_research_graph(config: ResearchConfig | None = None):
    if config is None:
        config = ResearchConfig()
    from core.registry import get_tools_for_agent
    core_names = {t.name for t in CORE_TOOLS}
    dynamic = get_tools_for_agent("deep_research", exclude_names=core_names)
    all_tools = CORE_TOOLS + dynamic

    async def research_planner_node(state: ResearchState) -> dict:
        step_tools = _apply_tool_selection(state, all_tools)
        llm = get_llm("deep_research").bind_tools(step_tools)

        # --- progress tracking ---
        tracker = ProgressTracker.from_dict(state.get("progress", {})) if state.get("progress") else ProgressTracker(original_query=state["query"])
        tool_msgs = _latest_tool_messages(state["messages"])
        step = state["step_count"]

        # Find previous AI message for extracting tool call queries
        prev_ai = None
        for msg in reversed(state["messages"]):
            if isinstance(msg, AIMessage):
                prev_ai = msg
                break

        completed_qs, findings_extracted, failed_qs = extract_progress_from_tool_results(tool_msgs, prev_ai)
        for q in completed_qs:
            tracker.record_search(q, success=True)
        for q in failed_qs:
            tracker.record_search(q, success=False)
        for f in findings_extracted:
            tracker.record_finding(f)
        attention_block = tracker.build_attention_block()

        # --- three-tier context management ---
        ctx = ResearchContext.from_dict(state.get("research_context", {})) if state.get("research_context") else ResearchContext()
        new_entries = ctx.merge_tool_results(tool_msgs, step)
        await ctx.trigger_compression(step)
        prompt_context = ctx.build_prompt_context()

        # --- persist findings to external memory ---
        task_id = state.get("task_id", "")
        if task_id and new_entries:
            try:
                memory = ResearchMemory(task_id)
                for entry in new_entries:
                    memory.save_finding(entry.step, entry.tool_name, entry.query, entry.raw_content or entry.compact_content, entry.source_urls)
                if step > 0 and step % config.checkpoint_interval == 0:
                    memory.save_checkpoint(step, {
                        "research_context": ctx.to_dict(),
                        "step_count": step,
                        "progress": tracker.to_dict(),
                    })
            except Exception:
                log.warning("research_memory save failed at step %d", step, exc_info=True)

        # --- periodic structured summary ---
        if step > 0 and step % config.summary_interval == 0:
            try:
                summary = await _generate_structured_summary(state["query"], ctx, tracker)
                ctx.update_summary(summary)
                if task_id:
                    ResearchMemory(task_id).save_summary(step, summary)
                # --- extract and record gaps from summary ---
                for gap in extract_gaps_from_summary(summary):
                    tracker.record_gap(gap)
            except Exception:
                log.warning("summary generation failed at step %d", step, exc_info=True)

        # --- set ContextVar for memory tools ---
        if task_id:
            try:
                set_research_context(ResearchMemory(task_id), step)
            except Exception:
                set_research_context(None, step)
        else:
            set_research_context(None, step)

        messages = _build_research_messages(
            state["query"],
            prompt_context,
            state.get("report_profile", DEFAULT_REPORT_PROFILE),
            attention_block=attention_block,
        )
        start = time.monotonic()
        response = await llm.ainvoke(messages)
        elapsed = time.monotonic() - start
        # --- token tracking ---
        usage = getattr(response, "usage_metadata", None) or {}
        if isinstance(usage, dict):
            tracker.record_token_usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0))

        log.info(
            "research_step step=%d elapsed=%.1fs tool_calls=%d "
            "context_raw=%d context_compact=%d context_summary=%d "
            "completed_searches=%d failed_searches=%d findings=%d gaps=%d",
            step, elapsed,
            len(getattr(response, "tool_calls", []) or []),
            sum(len(e.raw_content) for e in ctx.entries),
            sum(len(e.compact_content) for e in ctx.entries),
            len(ctx.summary),
            len(tracker.completed_searches),
            len(tracker.failed_searches),
            len(tracker.key_findings_so_far),
            len(tracker.remaining_gaps),
        )

        return {
            "messages": [response],
            "step_count": step + 1,
            "research_context": ctx.to_dict(),
            "progress": tracker.to_dict(),
        }

    async def research_tools_node(state: ResearchState) -> dict:
        return await ToolNode(all_tools).ainvoke(state)

    def _should_continue(state: ResearchState) -> Literal["tools", "finish"]:
        if state["step_count"] >= config.max_steps:
            return "finish"
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return "finish"

    g = StateGraph(ResearchState)
    g.add_node("planner", research_planner_node)
    g.add_node("tools", research_tools_node)
    g.add_node("finish", research_finish)
    g.set_entry_point("planner")
    g.add_conditional_edges("planner", _should_continue, {"tools": "tools", "finish": "finish"})
    g.add_edge("tools", "planner")
    g.add_edge("finish", END)
    return g.compile()


def build_hierarchical_research_graph():
    """Build a graph with plan generation → subtask routing → research loop → synthesis."""
    from core.registry import get_tools_for_agent
    core_names = {t.name for t in CORE_TOOLS}
    dynamic = get_tools_for_agent("deep_research", exclude_names=core_names)
    all_tools = CORE_TOOLS + dynamic

    async def plan_generator_node(state: ResearchState) -> dict:
        """Generate a research plan with subtasks."""
        tracker = ProgressTracker.from_dict(state.get("progress", {})) if state.get("progress") else ProgressTracker(original_query=state["query"])
        try:
            plan = await generate_research_plan(
                state["query"],
                memory_context="",
                report_profile=state.get("report_profile", DEFAULT_REPORT_PROFILE),
            )
            tracker.clarified_goal = plan.clarified_goal
            for st in plan.subtasks:
                tracker.update_subtask(st.id, st.status)
        except Exception:
            log.warning("plan generation failed, creating single-task plan", exc_info=True)
            from capabilities.planner import ResearchSubtask
            plan = ResearchPlan(
                original_query=state["query"],
                clarified_goal=state["query"],
                subtasks=[ResearchSubtask(
                    id="sub_1", topic=state["query"], search_angles=[state["query"]],
                    priority=1, max_rounds=5, completion_criteria="信息足够回答原始问题",
                )],
            )
        return {
            "research_plan": plan.to_dict(),
            "progress": tracker.to_dict(),
        }

    async def subtask_router_node(state: ResearchState) -> dict:
        """Pick next subtask from plan."""
        plan = ResearchPlan.from_dict(state.get("research_plan", {}))
        next_st = get_next_subtask(plan)
        if next_st is None:
            return {"current_subtask": {}}

        next_st.status = "in_progress"
        # Update plan
        for st in plan.subtasks:
            if st.id == next_st.id:
                st.status = "in_progress"
                break

        # Update tracker
        tracker = ProgressTracker.from_dict(state.get("progress", {})) if state.get("progress") else ProgressTracker(original_query=state["query"])
        tracker.update_subtask(next_st.id, "in_progress")

        # Inject subtask context into messages
        subtask_prompt = (
            f"当前子任务：{next_st.topic}\n"
            f"搜索角度：{', '.join(next_st.search_angles)}\n"
            f"完成标准：{next_st.completion_criteria}\n"
            f"请聚焦于该子任务进行研究。"
        )
        return {
            "current_subtask": next_st.to_dict(),
            "research_plan": plan.to_dict(),
            "messages": [HumanMessage(content=subtask_prompt)],
            "progress": tracker.to_dict(),
        }

    async def subtask_judge_node(state: ResearchState) -> dict:
        """Mark current subtask as completed and merge findings."""
        plan = ResearchPlan.from_dict(state.get("research_plan", {}))
        current = state.get("current_subtask", {})
        if not current:
            return {"current_subtask": {}}

        subtask_id = current.get("id", "")
        for st in plan.subtasks:
            if st.id == subtask_id:
                st.status = "completed"
                ctx = ResearchContext.from_dict(state.get("research_context", {}))
                st.findings_summary = ctx.build_final_context()[:2000]
                break

        tracker = ProgressTracker.from_dict(state.get("progress", {})) if state.get("progress") else ProgressTracker(original_query=state["query"])
        tracker.update_subtask(subtask_id, "completed")

        return {
            "current_subtask": {},
            "research_plan": plan.to_dict(),
            "progress": tracker.to_dict(),
        }

    def subtask_should_continue(state: ResearchState) -> str:
        """Route: if current_subtask is set → planner, else check plan → synthesize."""
        if state.get("current_subtask"):
            return "planner"
        plan_dict = state.get("research_plan", {})
        if plan_dict:
            plan = ResearchPlan.from_dict(plan_dict)
            if is_plan_complete(plan):
                return "synthesize"
        return "synthesize"

    async def hierarchical_planner_node(state: ResearchState) -> dict:
        """Research planner that respects subtask scope."""
        step_tools = _apply_tool_selection(state, all_tools)
        llm = get_llm("deep_research").bind_tools(step_tools)

        tracker = ProgressTracker.from_dict(state.get("progress", {})) if state.get("progress") else ProgressTracker(original_query=state["query"])
        tool_msgs = _latest_tool_messages(state["messages"])
        step = state["step_count"]

        prev_ai = None
        for msg in reversed(state["messages"]):
            if isinstance(msg, AIMessage):
                prev_ai = msg
                break

        completed_qs, findings_extracted, failed_qs = extract_progress_from_tool_results(tool_msgs, prev_ai)
        for q in completed_qs:
            tracker.record_search(q, success=True)
        for q in failed_qs:
            tracker.record_search(q, success=False)
        for f in findings_extracted:
            tracker.record_finding(f)
        attention_block = tracker.build_attention_block()

        ctx = ResearchContext.from_dict(state.get("research_context", {})) if state.get("research_context") else ResearchContext()
        ctx.merge_tool_results(tool_msgs, step)
        await ctx.trigger_compression(step)
        prompt_context = ctx.build_prompt_context()

        messages = _build_research_messages(
            state["query"],
            prompt_context,
            state.get("report_profile", DEFAULT_REPORT_PROFILE),
            attention_block=attention_block,
        )
        response = await llm.ainvoke(messages)
        return {
            "messages": [response],
            "step_count": step + 1,
            "research_context": ctx.to_dict(),
            "progress": tracker.to_dict(),
        }

    async def hierarchical_tools_node(state: ResearchState) -> dict:
        return await ToolNode(all_tools).ainvoke(state)

    def hierarchical_should_continue(state: ResearchState) -> str:
        """In hierarchical mode, 'finish' maps to subtask_judge instead of END."""
        if state["step_count"] >= 15:
            return "subtask_judge"
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return "subtask_judge"

    g = StateGraph(ResearchState)
    g.add_node("plan_generator", plan_generator_node)
    g.add_node("subtask_router", subtask_router_node)
    g.add_node("planner", hierarchical_planner_node)
    g.add_node("tools", hierarchical_tools_node)
    g.add_node("subtask_judge", subtask_judge_node)
    g.add_node("synthesize", research_finish)

    g.set_entry_point("plan_generator")
    g.add_edge("plan_generator", "subtask_router")
    g.add_conditional_edges("subtask_router", subtask_should_continue, {"planner": "planner", "synthesize": "synthesize"})
    g.add_conditional_edges("planner", hierarchical_should_continue, {"tools": "tools", "subtask_judge": "subtask_judge"})
    g.add_edge("tools", "planner")
    g.add_edge("subtask_judge", "subtask_router")
    g.add_edge("synthesize", END)

    return g.compile()


def build_parallel_research_graph():
    """Build a graph with plan generation → parallel workers → synthesis."""
    from core.registry import get_tools_for_agent
    from capabilities.context_manager import FindingEntry
    core_names = {t.name for t in CORE_TOOLS}
    dynamic = get_tools_for_agent("deep_research", exclude_names=core_names)
    all_tools = CORE_TOOLS + dynamic

    async def plan_generator_node(state: ResearchState) -> dict:
        """Generate a research plan with subtasks."""
        tracker = ProgressTracker.from_dict(state.get("progress", {})) if state.get("progress") else ProgressTracker(original_query=state["query"])
        try:
            plan = await generate_research_plan(
                state["query"],
                memory_context="",
                report_profile=state.get("report_profile", DEFAULT_REPORT_PROFILE),
            )
            tracker.clarified_goal = plan.clarified_goal
            for st in plan.subtasks:
                tracker.update_subtask(st.id, st.status)
        except Exception:
            log.warning("plan generation failed, creating single-task plan", exc_info=True)
            from capabilities.planner import ResearchSubtask as _RS
            plan = ResearchPlan(
                original_query=state["query"],
                clarified_goal=state["query"],
                subtasks=[_RS(
                    id="sub_1", topic=state["query"], search_angles=[state["query"]],
                    priority=1, max_rounds=5, completion_criteria="信息足够回答原始问题",
                )],
            )
        return {
            "research_plan": plan.to_dict(),
            "progress": tracker.to_dict(),
        }

    async def parallel_research_node(state: ResearchState) -> dict:
        """Run all subtasks in parallel waves."""
        from agents.research_worker import coordinate_research

        plan = ResearchPlan.from_dict(state.get("research_plan", {}))
        task_id = state.get("task_id", "")
        memory = ResearchMemory(task_id) if task_id else None

        results = await coordinate_research(plan, all_tools, memory)

        # Build ResearchContext from parallel results
        ctx = ResearchContext.from_dict(state.get("research_context", {})) if state.get("research_context") else ResearchContext()
        for sid, worker_findings in results.items():
            if worker_findings:
                ctx.add_entry(FindingEntry(
                    step=0, tool_name=f"worker_{sid}", query=sid,
                    raw_content=worker_findings,
                ))

        # Try LLM synthesis for the summary
        try:
            synthesis = await _synthesize_parallel_findings(
                state["query"], results,
                state.get("report_profile", DEFAULT_REPORT_PROFILE),
            )
            ctx.update_summary(synthesis)
        except Exception:
            log.warning("parallel synthesis failed", exc_info=True)

        return {
            "research_context": ctx.to_dict(),
            "research_plan": plan.to_dict(),
        }

    g = StateGraph(ResearchState)
    g.add_node("plan_generator", plan_generator_node)
    g.add_node("parallel_research", parallel_research_node)
    g.add_node("finish", research_finish)

    g.set_entry_point("plan_generator")
    g.add_edge("plan_generator", "parallel_research")
    g.add_edge("parallel_research", "finish")
    g.add_edge("finish", END)

    return g.compile()
