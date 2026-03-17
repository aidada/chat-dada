"""
Deep Research Agent — multi-round research with web search + academic search.
Upgraded version of search_agent with academic paper support.
"""
import logging
from dataclasses import dataclass
from typing import Annotated, Literal
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from content_utils import extract_result_text, extract_text_content, normalize_markdown_report
from logger import log_async
from models import get_browser_use_llm, get_llm
from task_interaction import ask_user

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


CORE_TOOLS = [web_search, academic_search, browser_navigate, ask_user_clarification]


class ResearchState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    query: str
    step_count: int
    findings: str
    report_profile: str


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
    findings = _merge_findings(
        state.get("findings", ""),
        _summarize_tool_messages(_latest_tool_messages(state["messages"])),
    )
    messages = _build_research_messages(
        state["query"],
        findings,
        state.get("report_profile", DEFAULT_REPORT_PROFILE),
    )
    response = await llm.ainvoke(messages)
    return {"messages": [response], "step_count": state["step_count"] + 1, "findings": findings}


async def research_tools(state: ResearchState) -> dict:
    return await ToolNode(CORE_TOOLS).ainvoke(state)


def research_finish(state: ResearchState) -> dict:
    fallback = normalize_markdown_report(extract_text_content(state.get("findings", "")))
    for msg in reversed(state["messages"]):
        if not isinstance(msg, AIMessage):
            continue
        text = normalize_markdown_report(extract_text_content(msg))
        if text:
            return {"findings": text}

    if fallback:
        log.warning("deep_research finish found no textual AIMessage content; falling back to accumulated findings")
    else:
        log.warning("deep_research finish found no textual AIMessage content or accumulated findings")
    return {"findings": fallback}


def research_should_continue(state: ResearchState) -> Literal["tools", "finish"]:
    if state["step_count"] >= 15:
        return "finish"
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "finish"


def build_research_graph():
    from registry import get_tools_for_agent
    core_names = {t.name for t in CORE_TOOLS}
    dynamic = get_tools_for_agent("deep_research", exclude_names=core_names)
    all_tools = CORE_TOOLS + dynamic

    async def research_planner_node(state: ResearchState) -> dict:
        llm = get_llm("deep_research").bind_tools(all_tools)
        findings = _merge_findings(
            state.get("findings", ""),
            _summarize_tool_messages(_latest_tool_messages(state["messages"])),
        )
        messages = _build_research_messages(
            state["query"],
            findings,
            state.get("report_profile", DEFAULT_REPORT_PROFILE),
        )
        response = await llm.ainvoke(messages)
        return {"messages": [response], "step_count": state["step_count"] + 1, "findings": findings}

    async def research_tools_node(state: ResearchState) -> dict:
        return await ToolNode(all_tools).ainvoke(state)

    g = StateGraph(ResearchState)
    g.add_node("planner", research_planner_node)
    g.add_node("tools", research_tools_node)
    g.add_node("finish", research_finish)
    g.set_entry_point("planner")
    g.add_conditional_edges("planner", research_should_continue, {"tools": "tools", "finish": "finish"})
    g.add_edge("tools", "planner")
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
    graph = build_research_graph()
    task_prompt = f"请深入研究以下主题：\n{query}"
    if memory_context:
        task_prompt = f"{memory_context}\n\n{task_prompt}"
    state = {
        "messages": [HumanMessage(content=task_prompt)],
        "query": query,
        "step_count": 0,
        "findings": "",
        "report_profile": report_profile,
    }
    result = await graph.ainvoke(state)
    findings = extract_result_text(result.get("findings", ""))
    if findings:
        findings = await _rewrite_final_report(query, findings, report_profile)
    return {"status": "ok", "result": findings}


def _build_research_messages(query: str, findings: str, report_profile: str = DEFAULT_REPORT_PROFILE) -> list[BaseMessage]:
    notes = findings or "(暂无研究笔记)"
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


def _summarize_tool_messages(messages: list[ToolMessage]) -> str:
    if not messages:
        return ""

    sections: list[str] = []
    for message in messages:
        tool_name = str(getattr(message, "name", "") or "tool")
        text = _truncate_text(_message_text(message), 700)
        if not text:
            continue
        sections.append(f"### {tool_name}\n{text}")
    return "\n\n".join(sections[:2])


def _merge_findings(existing: str, incoming: str, max_chars: int = 6000) -> str:
    parts = [part.strip() for part in (existing, incoming) if part and part.strip()]
    if not parts:
        return ""
    merged = "\n\n".join(parts)
    if len(merged) <= max_chars:
        return merged
    return merged[-max_chars:]


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
