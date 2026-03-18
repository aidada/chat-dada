"""
Deep Research Agent — multi-round research with web search + academic search.
Upgraded version of search_agent with academic paper support.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Annotated, Literal
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from content_utils import extract_result_text, extract_text_content, normalize_markdown_report
from context_manager import ResearchContext
from logger import log_async
from models import get_browser_use_llm, get_llm, response_text
from progress_tracker import ProgressTracker, extract_gaps_from_summary, extract_progress_from_tool_results
from research_memory import ResearchMemory
from research_planner import ResearchPlan, generate_research_plan, get_next_subtask, is_plan_complete
from task_interaction import ask_user
from tools.research_notes import save_research_note, recall_research_notes, set_research_context

log = logging.getLogger("chatdada.agent")

try:
    from langchain_community.tools.tavily_search import TavilySearchResults
    HAS_TAVILY = True
except ImportError:
    HAS_TAVILY = False


@tool
async def web_search(query: str) -> str:
    """用 Tavily 搜索互联网，适合研究型查询和提取较完整的摘要。"""
    if HAS_TAVILY:
        search = TavilySearchResults(max_results=5)
        results = await search.ainvoke(query)
        return "\n\n".join(f"[{r['url']}]\n{r['content']}" for r in results)
    return f"(TAVILY_API_KEY not configured, skipping '{query}')"


@tool
async def academic_search(query: str) -> str:
    """搜索学术论文（Semantic Scholar + arXiv）。"""
    from tools.academic_search import run as search_academic
    result = await search_academic({"query": query})
    return result.get("result", "No results")


@tool
async def browser_navigate(task_description: str) -> str:
    """控制浏览器完成复杂网页任务。"""
    from browser_use import Agent as BrowserAgent
    from browser_use import BrowserSession as Browser
    from browser_use import BrowserProfile as BrowserConfig
    browser = Browser(browser_profile=BrowserConfig(headless=True))
    llm = get_browser_use_llm("deep_research")
    agent = BrowserAgent(task=task_description, llm=llm, browser=browser, max_actions_per_step=5)
    result = await agent.run(max_steps=10)
    final = result.final_result() if hasattr(result, "final_result") else str(result)
    return final or "Browser task done."


@tool
async def ask_user_clarification(
    question: str,
    why: str = "",
    placeholder: str = "",
) -> str:
    """向用户确认研究方向或判断标准。仅当歧义会显著影响检索方向时调用，且最多调用一次。"""
    answer = await ask_user(question, context=why, placeholder=placeholder)
    if answer is None:
        return "用户交互当前不可用，请基于现有任务描述继续研究。"
    return answer


CORE_TOOLS = [web_search, academic_search, browser_navigate, ask_user_clarification,
              save_research_note, recall_research_notes]


class ResearchState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    query: str
    step_count: int
    report_profile: str
    research_context: dict      # serialized ResearchContext — 唯一数据源
    task_id: str                # research memory task ID
    progress: dict              # serialized ProgressTracker
    research_plan: dict         # serialized ResearchPlan (P1-1)
    current_subtask: dict       # current subtask or {} (P1-1)


CHECKPOINT_INTERVAL = 5
SUMMARY_INTERVAL = 6


@dataclass
class ResearchConfig:
    max_steps: int = 15
    checkpoint_interval: int = 5
    summary_interval: int = 6
    max_parallel_workers: int = 3
    raw_content_threshold: int = 8000
    compact_snippet_length: int = 200

    @classmethod
    def from_dict(cls, data: dict) -> "ResearchConfig":
        return cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})


DEFAULT_REPORT_PROFILE = "default"
ACADEMIC_PAPER_GUIDANCE_PROFILE = "academic_paper_guidance"


@dataclass(frozen=True)
class ReportProfile:
    name: str
    research_addendum: str
    final_addendum: str
    final_sections: tuple[str, ...]


REPORT_PROFILES: dict[str, ReportProfile] = {
    DEFAULT_REPORT_PROFILE: ReportProfile(
        name=DEFAULT_REPORT_PROFILE,
        research_addendum="",
        final_addendum=(
            "默认输出是一份问题导向的研究报告。\n"
            "你必须使用 Markdown 二级标题，且至少包含：\n"
            "`## 直接结论`\n"
            "`## 证据链`\n"
            "`## 机理与成立条件`\n"
            "`## 工程判断`\n"
            "`## 限制与证据缺口`\n"
            "`## 参考来源`\n"
            "开头必须是 `## 直接结论`，用 3-5 句直接回答“是否成立、为什么、依赖什么条件、工程上如何理解”。"
        ),
        final_sections=(
            "## 直接结论",
            "## 证据链",
            "## 机理与成立条件",
            "## 工程判断",
            "## 限制与证据缺口",
            "## 参考来源",
        ),
    ),
    ACADEMIC_PAPER_GUIDANCE_PROFILE: ReportProfile(
        name=ACADEMIC_PAPER_GUIDANCE_PROFILE,
        research_addendum=(
            "当前任务是“科研论文写作导向”的深度研究，不是普通报告。\n"
            "检索时除结论本身外，还要额外提取：\n"
            "1. 问题背景与研究动机如何铺垫；\n"
            "2. 主流方法脉络、代表性论文及其局限；\n"
            "3. 现有工作尚未解决的研究空白；\n"
            "4. 能支撑后续论文写作的 claim、证据强度与风险边界；\n"
            "5. 后续论文必须补的实验、baseline、ablation、误差分析与图表。\n"
            "如果证据不足以支撑论文写作建议，必须明确标记“需要补证据后再写”。"
        ),
        final_addendum=(
            "你现在输出的是“为后续论文写作服务的科研综述型报告”，不是普通研究报告。\n"
            "硬性要求：\n"
            "1. 先写 `## 文献综述正文`，使用连续段落，尽量模拟论文 introduction / 绪论 / related work 的写法，而不是项目符号堆砌。\n"
            "2. 每个事实判断、方法归纳、趋势结论后都要尽量附引用编号，如 [1]、[2]；如果研究笔记里的出处信息不足以稳定编号，则在句末保留来源 URL 或明确说明出处信息不完整。\n"
            "3. 综述之后必须单独给出“对后续论文写作的明确建议”，且必须回答：核心问题怎么定义、创新点怎么表述、Introduction 如何分段、Experiment 还需要补哪些关键实验。\n"
            "4. 必须把“当前可以主张的点”和“当前不能写过头的点”分开写，防止把摘要级证据写成强结论。\n"
            "5. 所有写作建议都必须基于前文文献证据，不得脱离证据自由发挥。\n"
            "6. 如果某项建议依赖尚未补齐的实验、公式或正文细节，必须明确标注“需要补证据后再写”。\n"
            "7. 末尾必须有 `## 参考文献`，按编号或可识别顺序列出题目、作者、年份、来源、链接；若信息缺失，必须标注缺失项。"
        ),
        final_sections=(
            "## 文献综述正文",
            "## 研究空白与可切入点",
            "## 对后续论文写作的明确建议",
            "## 当前可以主张的点",
            "## 当前不能写过头的点",
            "## 建议的论文结构",
            "## 建议补充的实验与材料",
            "## 参考文献",
        ),
    ),
}

REPORT_PROFILE_ALIASES = {
    "general": DEFAULT_REPORT_PROFILE,
    "default": DEFAULT_REPORT_PROFILE,
    "academic": ACADEMIC_PAPER_GUIDANCE_PROFILE,
    "academic_intro": ACADEMIC_PAPER_GUIDANCE_PROFILE,
    "academic_paper": ACADEMIC_PAPER_GUIDANCE_PROFILE,
    "academic_paper_guidance": ACADEMIC_PAPER_GUIDANCE_PROFILE,
    "paper": ACADEMIC_PAPER_GUIDANCE_PROFILE,
}

ACADEMIC_PROFILE_KEYWORDS = (
    "论文",
    "paper",
    "引言",
    "introduction",
    "绪论",
    "related work",
    "文献综述",
    "literature review",
    "综述",
    "创新点",
    "research gap",
    "研究空白",
    "baseline",
    "ablation",
    "实验设计",
    "投稿",
    "审稿",
    "reference",
    "参考文献",
    "科研写作",
    "后续论文怎么写",
)


BASE_RESEARCH_SYSTEM = """你是一个专业的深度研究员。你的任务是围绕用户提出的问题做多轮检索，并给出直接、严谨、可执行的研究结论。

策略：
1. 先识别用户真正想知道的判断题、机理问题、工程问题。
1.1 如果用户问题存在关键歧义，而且这种歧义会改变检索方向、评价指标或工程判断，先调用 `ask_user_clarification` 追问一次；问题必须短、具体、可操作，且最多追问一次。
2. 优先搜索能直接回答该问题的论文、综述、实验结果、方法细节，不要只堆背景材料。
3. 使用 web_search 获取较完整摘要，使用 academic_search 找论文，必要时用 browser_navigate 抓具体页面。
4. 多角度搜索：中文 + 英文关键词；支持词、反对词、边界条件都要覆盖。
5. 每一轮最多调用 1-2 个工具，优先补齐最关键的信息缺口。
6. 如果缺少实验数据、公式、可观测性条件、工程限制，默认信息仍不足，不要过早停止。

最终输出要求：
1. 必须先回答用户真正关心的问题，而不是先铺陈背景。
2. 关键结论后要给来源 URL，或明确说明“当前仅有摘要级证据”。
3. 如果没有检索到关键实验数字、公式或正文证据，不得模糊带过，必须明确写出缺口。"""


BASE_FINAL_REPORT_SYSTEM = """你是研究报告编辑。请把已有研究笔记改写成一份直接回答用户问题的最终报告。

硬性要求：
1. 只能基于提供的研究笔记重写，不要新增笔记里没有的事实。
2. 结论必须紧扣用户问题本身，避免泛泛背景综述。
3. 如果证据只有摘要级、缺少实验数字或缺少正文公式，必须明确标注证据强度和缺口。"""


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


async def _generate_structured_summary(query: str, ctx: ResearchContext, tracker: ProgressTracker) -> str:
    """Generate a structured summary of research progress using the orchestrator LLM."""
    llm = get_llm("orchestrator")

    # Collect entry content (each ≤500 chars, max 20 entries)
    entry_texts: list[str] = []
    for entry in ctx.entries[-20:]:
        content = entry.raw_content or entry.compact_content
        if content:
            entry_texts.append(content[:500])

    all_entries = "\n---\n".join(entry_texts) if entry_texts else "(无内容)"

    system_prompt = (
        "你是一个研究进度总结器。请根据提供的研究条目内容，生成一份结构化的研究摘要，包含：\n"
        "1. 已覆盖子主题\n"
        "2. 证据强度评估\n"
        "3. 尚未覆盖的缺口\n"
        "4. 核心发现\n"
        "5. 下一步建议\n\n"
        "总结必须≤800字。"
    )
    human_prompt = (
        f"研究主题：{query}\n\n"
        f"已完成搜索：{', '.join(tracker.completed_searches[-10:]) or '(无)'}\n\n"
        f"研究条目内容：\n{all_entries}"
    )

    resp = await _retry_async(llm.ainvoke, [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prompt),
    ])
    return response_text(resp)


async def _retry_async(coro_fn, *args, max_retries: int = 2, delay: float = 1.0, **kwargs):
    """重试异步函数调用，仅对可恢复错误重试。

    可恢复错误：OSError, TimeoutError, ConnectionError
    不可恢复错误：ValueError, TypeError, KeyError → 直接抛出
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except (OSError, TimeoutError, ConnectionError) as e:
            last_exc = e
            if attempt < max_retries:
                await asyncio.sleep(delay * (attempt + 1))
                log.info("Retrying %s (attempt %d/%d)", coro_fn.__name__, attempt + 2, max_retries + 1)
            continue
        except Exception:
            raise
    raise last_exc


async def _synthesize_parallel_findings(
    query: str,
    subtask_results: dict[str, str],
    report_profile: str,
) -> str:
    """用 orchestrator LLM 合并多个子任务的发现。

    - 去重重叠内容
    - 标注矛盾发现
    - 按报告模板组织结构
    - 输出≤3000字
    """
    llm = get_llm("orchestrator")

    entries = []
    for sid, findings in subtask_results.items():
        entries.append(f"## 子任务 {sid}\n{findings[:1500]}")
    all_entries = "\n\n---\n\n".join(entries)

    profile = _get_report_profile(report_profile)
    sections = "\n".join(f"- {s}" for s in profile.final_sections)

    system_prompt = (
        "你是一个研究合成器。请把多个子任务的研究发现合并成一份结构化报告。\n\n"
        "要求：\n"
        "1. 去除重复内容，保留信息量最大的表述\n"
        '2. 如果子任务之间有矛盾发现，明确标注"[矛盾]"并列出各方证据\n'
        "3. 按以下报告结构组织：\n"
        f"{sections}\n"
        "4. 合并后总长度≤3000字\n"
        "5. 每个结论保留来源标注"
    )

    resp = await _retry_async(llm.ainvoke, [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"研究主题：{query}\n\n子任务发现：\n{all_entries}"),
    ])
    return response_text(resp)


def build_research_graph(config: ResearchConfig | None = None):
    if config is None:
        config = ResearchConfig()
    from registry import get_tools_for_agent
    core_names = {t.name for t in CORE_TOOLS}
    dynamic = get_tools_for_agent("deep_research", exclude_names=core_names)
    all_tools = CORE_TOOLS + dynamic

    async def research_planner_node(state: ResearchState) -> dict:
        llm = get_llm("deep_research").bind_tools(all_tools)

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
    from registry import get_tools_for_agent
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
            plan = ResearchPlan(
                original_query=state["query"],
                clarified_goal=state["query"],
                subtasks=[__import__("research_planner").ResearchSubtask(
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
        llm = get_llm("deep_research").bind_tools(all_tools)

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
    from registry import get_tools_for_agent
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
            from research_planner import ResearchSubtask as _RS
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
        from context_manager import FindingEntry

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


@log_async("agent", "deep_research")
async def run(input_data) -> dict:
    if isinstance(input_data, str):
        query = input_data
        memory_context = ""
        requested_report_profile = ""
    elif isinstance(input_data, dict):
        raw_query = input_data.get("query", input_data.get("search_query", str(input_data)))
        if isinstance(raw_query, dict):
            query = str(
                raw_query.get("query")
                or raw_query.get("task")
                or raw_query.get("search_query")
                or raw_query
            )
            requested_report_profile = str(
                input_data.get("report_profile")
                or raw_query.get("report_profile")
                or ""
            )
        else:
            query = str(raw_query)
            requested_report_profile = str(input_data.get("report_profile", "") or "")
        memory_context = input_data.get("memory_context", "")
    else:
        query = str(input_data)
        memory_context = ""
        requested_report_profile = ""

    report_profile = _resolve_report_profile(query, requested_report_profile)

    # --- input validation ---
    if not query or not query.strip():
        return {"status": "error", "result": "研究查询不能为空。"}
    query = query.strip()
    if len(query) > 10000:
        query = query[:10000]
        log.warning("query truncated to 10000 chars")
    if memory_context and len(memory_context) > 50000:
        memory_context = memory_context[:50000]
        log.warning("memory_context truncated to 50000 chars")

    # --- parse config ---
    config = ResearchConfig()
    if isinstance(input_data, dict) and input_data.get("config"):
        config = ResearchConfig.from_dict(input_data["config"])

    # --- check for resume from checkpoint ---
    resume_task_id = ""
    if isinstance(input_data, dict):
        resume_task_id = input_data.get("resume_task_id", "")

    resumed_state: dict | None = None
    if resume_task_id:
        try:
            memory = ResearchMemory(resume_task_id)
            checkpoint = memory.load_checkpoint()
            if checkpoint:
                meta = memory.load_meta()
                old_query = (meta or {}).get("query", "")
                # Detect query mismatch on resume
                if query and query != str(input_data) and old_query and query != old_query:
                    log.warning("Resume query mismatch: checkpoint='%s', new='%s'. Using new query.",
                                old_query[:50], query[:50])
                elif not query or query == str(input_data):
                    query = old_query or query
                if not requested_report_profile:
                    report_profile = (meta or {}).get("report_profile", report_profile)
                resumed_state = {
                    "step_count": checkpoint.get("step_count", 0),
                    "research_context": checkpoint.get("research_context", {}),
                    "progress": checkpoint.get("progress", {}),
                }
                log.info("Resumed research from checkpoint for task %s at step %d", resume_task_id, resumed_state["step_count"])
        except Exception:
            log.warning("Failed to resume from checkpoint %s, starting fresh", resume_task_id, exc_info=True)

    # --- initialize external memory ---
    if resume_task_id and resumed_state:
        task_id = resume_task_id
    else:
        task_id = f"research_{uuid.uuid4().hex[:12]}"
        try:
            ResearchMemory(task_id).init(query, report_profile)
        except Exception:
            log.warning("research_memory init failed for %s", task_id, exc_info=True)
            task_id = ""

    graph = build_research_graph(config)
    # --- select graph mode ---
    use_hierarchical = isinstance(input_data, dict) and input_data.get("hierarchical", False)
    use_parallel = isinstance(input_data, dict) and input_data.get("parallel", False)
    if use_parallel:
        graph = build_parallel_research_graph()
    elif use_hierarchical:
        graph = build_hierarchical_research_graph()
    task_prompt = f"请深入研究以下主题：\n{query}"
    if memory_context:
        task_prompt = f"{memory_context}\n\n{task_prompt}"
    state = {
        "messages": [HumanMessage(content=task_prompt)],
        "query": query,
        "step_count": resumed_state["step_count"] if resumed_state else 0,
        "report_profile": report_profile,
        "research_context": resumed_state["research_context"] if resumed_state else {},
        "task_id": task_id,
        "progress": resumed_state["progress"] if resumed_state else {},
        "research_plan": {},
        "current_subtask": {},
    }
    result = await graph.ainvoke(state)
    # Extract final text from result
    final_text = result.get("_final_text", "")
    if not final_text:
        ctx = ResearchContext.from_dict(result.get("research_context", {}))
        final_text = ctx.build_final_context()
    final_text = extract_result_text(final_text)
    if final_text:
        final_text = await _rewrite_final_report(query, final_text, report_profile)

    # --- persist final report ---
    if task_id:
        try:
            ResearchMemory(task_id).save_final_report(final_text)
        except Exception:
            log.warning("research_memory save_final_report failed", exc_info=True)

    return {"status": "ok", "result": final_text}


def _build_research_messages(query: str, context: str, report_profile: str = DEFAULT_REPORT_PROFILE, attention_block: str = "") -> list[BaseMessage]:
    notes = context or "(暂无研究笔记)"
    profile = _get_report_profile(report_profile)
    section_requirements = "\n".join(f"   `{section}`" for section in profile.final_sections)
    prompt = (
        f"研究主题：{query}\n\n"
        f"当前输出模板：{profile.name}\n\n"
        f"当前研究笔记（已压缩）：\n{notes}\n\n"
        "请基于当前笔记决定下一步：\n"
        "0. 如果当前笔记里还没有用户澄清，而研究目标、评价维度或输出重点存在关键歧义，先调用 ask_user_clarification；最多一次。\n"
        "1. 如果还缺少以下任一项，就继续调用最必要的 1-2 个工具：直接结论、关键证据、成立条件、工程限制、关键数据或明确缺口。\n"
        "2. 如果信息已经足够，直接输出最终研究报告。\n"
        "3. 不要重复已经完成的搜索。\n"
        "4. 最终报告必须围绕用户问题本身回答：命题是否成立、为什么、依赖哪些条件、工程上如何落地、还有哪些证据缺口。\n"
        "5. 不要把答案写成宽泛综述；优先给出判断、再给证据和限制。\n"
        "6. 最终报告至少要包含以下二级标题：\n"
        f"{section_requirements}"
    )
    if attention_block:
        prompt += f"\n\n{attention_block}"
    return [
        SystemMessage(content=_build_research_system(report_profile)),
        HumanMessage(content=prompt),
    ]


def _latest_tool_messages(messages: list[BaseMessage]) -> list[ToolMessage]:
    trailing: list[ToolMessage] = []
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            trailing.append(message)
            continue
        break
    trailing.reverse()
    return trailing


def _normalize_report_profile(report_profile: str | None) -> str:
    normalized = str(report_profile or "").strip().lower()
    if not normalized:
        return ""
    return REPORT_PROFILE_ALIASES.get(normalized, normalized)


def _get_report_profile(report_profile: str | None) -> ReportProfile:
    normalized = _normalize_report_profile(report_profile)
    return REPORT_PROFILES.get(normalized, REPORT_PROFILES[DEFAULT_REPORT_PROFILE])


def _looks_like_academic_paper_task(query: str) -> bool:
    lowered = str(query or "").lower()
    return any(keyword in lowered for keyword in ACADEMIC_PROFILE_KEYWORDS)


def _resolve_report_profile(query: str, requested_profile: str | None = None) -> str:
    normalized = _normalize_report_profile(requested_profile)
    if normalized in REPORT_PROFILES:
        return normalized
    if _looks_like_academic_paper_task(query):
        return ACADEMIC_PAPER_GUIDANCE_PROFILE
    return DEFAULT_REPORT_PROFILE


def _build_research_system(report_profile: str) -> str:
    profile = _get_report_profile(report_profile)
    if not profile.research_addendum:
        return BASE_RESEARCH_SYSTEM
    return f"{BASE_RESEARCH_SYSTEM}\n\n附加模板要求（{profile.name}）：\n{profile.research_addendum}"


def _build_final_report_system(report_profile: str) -> str:
    profile = _get_report_profile(report_profile)
    return f"{BASE_FINAL_REPORT_SYSTEM}\n\n附加模板要求（{profile.name}）：\n{profile.final_addendum}"


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content)


def _truncate_text(text: str, limit: int) -> str:
    compact = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


async def _rewrite_final_report(
    query: str,
    findings: str,
    report_profile: str = DEFAULT_REPORT_PROFILE,
) -> str:
    llm = get_llm("deep_research")
    messages = [
        SystemMessage(content=_build_final_report_system(report_profile)),
        HumanMessage(
            content=(
                f"用户问题：{query}\n\n"
                f"当前输出模板：{_get_report_profile(report_profile).name}\n\n"
                "请把下面的研究笔记改写成最终报告。只能基于这些笔记重写，不要新增事实。\n\n"
                f"研究笔记：\n{findings}"
            )
        ),
    ]
    try:
        response = await llm.ainvoke(messages)
    except Exception:
        return normalize_markdown_report(findings)
    return normalize_markdown_report(extract_text_content(response) or findings)
