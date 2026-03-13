"""
Deep Research Agent — multi-round research with web search + academic search.
Upgraded version of search_agent with academic paper support.
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


@tool
async def web_search(query: str) -> str:
    """搜索互联网获取最新信息。"""
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
    from browser_use.browser.browser import Browser, BrowserConfig
    browser = Browser(config=BrowserConfig(headless=True))
    llm = get_llm("search")
    agent = BrowserAgent(task=task_description, llm=llm, browser=browser, max_actions_per_step=5)
    result = await agent.run(max_steps=10)
    final = result.final_result() if hasattr(result, "final_result") else str(result)
    return final or "Browser task done."


CORE_TOOLS = [web_search, academic_search, browser_navigate]


class ResearchState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    query: str
    step_count: int
    findings: str


RESEARCH_SYSTEM = """你是一个专业的深度研究员。你的任务是对给定主题进行全面的多轮研究。

策略：
1. 先用 web_search 搜索关键词，了解背景
2. 用 academic_search 搜索相关学术论文
3. 如果需要抓取具体网页，用 browser_navigate
4. 多角度搜索：中文 + 英文关键词
5. 收集足够信息后，整理为结构化发现

输出格式：
- 领域概述
- 关键发现（带引用来源）
- 重要数据和统计
- 学术论文引用
- 结论和趋势"""


async def research_planner(state: ResearchState) -> dict:
    llm = get_llm("search").bind_tools(CORE_TOOLS)
    messages = [SystemMessage(content=RESEARCH_SYSTEM)] + state["messages"]
    response = await llm.ainvoke(messages)
    return {"messages": [response], "step_count": state["step_count"] + 1}


async def research_tools(state: ResearchState) -> dict:
    return await ToolNode(CORE_TOOLS).ainvoke(state)


def research_finish(state: ResearchState) -> dict:
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            return {"findings": str(msg.content)}
    return {"findings": ""}


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
        llm = get_llm("search").bind_tools(all_tools)
        messages = [SystemMessage(content=RESEARCH_SYSTEM)] + state["messages"]
        response = await llm.ainvoke(messages)
        return {"messages": [response], "step_count": state["step_count"] + 1}

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


async def run(input_data) -> dict:
    """Unified interface for registry dispatch."""
    if isinstance(input_data, str):
        query = input_data
    elif isinstance(input_data, dict):
        query = input_data.get("query", input_data.get("search_query", str(input_data)))
    else:
        query = str(input_data)

    graph = build_research_graph()
    state = {
        "messages": [HumanMessage(content=f"请深入研究以下主题：\n{query}")],
        "query": query,
        "step_count": 0,
        "findings": "",
    }
    result = await graph.ainvoke(state)
    return {"status": "ok", "result": result.get("findings", "")}
