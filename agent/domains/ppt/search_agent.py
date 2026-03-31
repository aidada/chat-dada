"""
Search Agent — sub-graph that searches the web and returns structured findings.
Uses its own LLM instance (configured in models.py as "search" role).
"""
from typing import Annotated, Literal
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from agent.capabilities.toolkits.browser_toolkit import browser_navigate_task
from core.content_utils import extract_text_content, normalize_markdown_report
from core.models import get_browser_use_llm, get_llm
from core.logger import log_async

try:
    from langchain_community.tools.tavily_search import TavilySearchResults
    HAS_TAVILY = True
except ImportError:
    HAS_TAVILY = False

# ── State ──
class SearchState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    query: str
    step_count: int
    findings: str


# ── Tools ──
@tool
async def web_search(query: str) -> str:
    """用 Tavily 搜索互联网，适合研究型查询和提取较完整的摘要。"""
    if HAS_TAVILY:
        search = TavilySearchResults(max_results=5)
        results = await search.ainvoke(query)
        return "\n\n".join(f"[{r['url']}]\n{r['content']}" for r in results)
    return f"(未配置 TAVILY_API_KEY，搜索 '{query}' 跳过)"


@tool
async def browser_navigate(task_description: str) -> str:
    """控制浏览器完成复杂网页任务：抓取动态内容、多步交互。"""
    llm = get_browser_use_llm("search")
    return await browser_navigate_task(task_description, role="search", llm=llm)


CORE_TOOLS = [web_search, browser_navigate]


# ── Nodes ──
SEARCH_SYSTEM = """你是一个专业的搜索研究员。你的任务是根据给定的搜索主题，通过多次搜索收集全面的信息。

策略：
1. brave_search 适合快速发现候选网页和来源
2. web_search 适合研究型查询和拿到更完整的摘要
3. 如果需要抓取具体网页内容，用 browser_navigate
4. 收集足够信息后，直接用文字总结你的发现

输出格式：将所有发现整理为结构化的要点，包含来源 URL。"""


async def search_planner(state: SearchState) -> dict:
    llm = get_llm("search").bind_tools(CORE_TOOLS)
    messages = [SystemMessage(content=SEARCH_SYSTEM)] + state["messages"]
    response = await llm.ainvoke(messages)
    return {"messages": [response], "step_count": state["step_count"] + 1}


async def search_tool_executor(state: SearchState) -> dict:
    return await ToolNode(CORE_TOOLS).ainvoke(state)


def search_finish(state: SearchState) -> dict:
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            return {"findings": normalize_markdown_report(extract_text_content(msg))}
    return {"findings": ""}


def search_should_continue(state: SearchState) -> Literal["tools", "finish"]:
    if state["step_count"] >= 10:
        return "finish"
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "finish"


# ── Graph ──
def build_search_graph():
    from core.registry import get_tools_for_agent
    core_names = {t.name for t in CORE_TOOLS}
    dynamic = get_tools_for_agent("search", exclude_names=core_names)
    all_tools = CORE_TOOLS + dynamic

    async def search_planner_node(state: SearchState) -> dict:
        llm = get_llm("search").bind_tools(all_tools)
        messages = [SystemMessage(content=SEARCH_SYSTEM)] + state["messages"]
        response = await llm.ainvoke(messages)
        return {"messages": [response], "step_count": state["step_count"] + 1}

    async def search_tool_executor_node(state: SearchState) -> dict:
        return await ToolNode(all_tools).ainvoke(state)

    g = StateGraph(SearchState)
    g.add_node("planner", search_planner_node)
    g.add_node("tools", search_tool_executor_node)
    g.add_node("finish", search_finish)
    g.set_entry_point("planner")
    g.add_conditional_edges("planner", search_should_continue, {"tools": "tools", "finish": "finish"})
    g.add_edge("tools", "planner")
    g.add_edge("finish", END)
    return g.compile()


@log_async("agent", "search_agent")
async def run_search(input_data) -> str:
    """Run the search agent and return findings as text."""
    if isinstance(input_data, str):
        query = input_data
        memory_context = ""
    elif isinstance(input_data, dict):
        query = input_data.get("query", input_data.get("search_query", str(input_data)))
        memory_context = input_data.get("memory_context", "")
    else:
        query = str(input_data)
        memory_context = ""

    graph = build_search_graph()
    task_prompt = f"请搜索以下主题并整理发现：\n{query}"
    if memory_context:
        task_prompt = f"{memory_context}\n\n{task_prompt}"
    state = {
        "messages": [HumanMessage(content=task_prompt)],
        "query": query,
        "step_count": 0,
        "findings": "",
    }
    result = await graph.ainvoke(state)
    return result.get("findings", "")
