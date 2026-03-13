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

from models import get_llm

try:
    from langchain_community.tools.tavily_search import TavilySearchResults
    HAS_TAVILY = True
except ImportError:
    HAS_TAVILY = False

from browser_use import Agent as BrowserAgent
from browser_use import BrowserSession as Browser
from browser_use import BrowserProfile as BrowserConfig


# ── State ──
class SearchState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    query: str
    step_count: int
    findings: str


# ── Tools ──
@tool
async def web_search(query: str) -> str:
    """搜索互联网获取最新信息。"""
    if HAS_TAVILY:
        search = TavilySearchResults(max_results=5)
        results = await search.ainvoke(query)
        return "\n\n".join(f"[{r['url']}]\n{r['content']}" for r in results)
    return f"(未配置 TAVILY_API_KEY，搜索 '{query}' 跳过)"


@tool
async def browser_navigate(task_description: str) -> str:
    """控制浏览器完成复杂网页任务：抓取动态内容、多步交互。"""
    browser = Browser(config=BrowserConfig(headless=True))
    llm = get_llm("search")
    agent = BrowserAgent(task=task_description, llm=llm, browser=browser, max_actions_per_step=5)
    result = await agent.run(max_steps=10)
    final = result.final_result() if hasattr(result, "final_result") else str(result)
    return final or "浏览器任务完成。"


CORE_TOOLS = [web_search, browser_navigate]


# ── Nodes ──
SEARCH_SYSTEM = """你是一个专业的搜索研究员。你的任务是根据给定的搜索主题，通过多次搜索收集全面的信息。

策略：
1. 先用 web_search 进行关键词搜索
2. 如果需要抓取具体网页内容，用 browser_navigate
3. 收集足够信息后，直接用文字总结你的发现

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
            return {"findings": str(msg.content)}
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
    from registry import get_tools_for_agent
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


async def run_search(query: str) -> str:
    """Run the search agent and return findings as text."""
    graph = build_search_graph()
    state = {
        "messages": [HumanMessage(content=f"请搜索以下主题并整理发现：\n{query}")],
        "query": query,
        "step_count": 0,
        "findings": "",
    }
    result = await graph.ainvoke(state)
    return result.get("findings", "")
