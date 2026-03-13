"""
LangGraph Agent 核心
架构: StateGraph → planner → tool_executor → planner → ... → finish

安装依赖:
  pip install langgraph langchain-anthropic langchain-community
  pip install browser-use tavily-python playwright
  playwright install chromium
"""

from typing import Callable, Awaitable, Annotated, Literal
from typing_extensions import TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from browser_use import Agent as BrowserAgent
from browser_use.browser.browser import Browser, BrowserConfig

try:
    from langchain_community.tools.tavily_search import TavilySearchResults
    HAS_TAVILY = True
except ImportError:
    HAS_TAVILY = False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. Agent 状态
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    task: str
    step_count: int
    final_result: str


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. 工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@tool
async def web_search(query: str) -> str:
    """搜索互联网获取最新信息。适合快速查询事实、新闻、文档。"""
    if HAS_TAVILY:
        search = TavilySearchResults(max_results=5)
        results = await search.ainvoke(query)
        return "\n\n".join(f"[{r['url']}]\n{r['content']}" for r in results)
    return f"(未配置 TAVILY_API_KEY，搜索 '{query}' 跳过)"


@tool
async def browser_navigate(task_description: str) -> str:
    """
    控制浏览器完成复杂网页任务：打开网页、点击、填表、抓取动态内容。
    适合需要多步骤交互的场景（简单搜索请用 web_search）。
    """
    browser = Browser(config=BrowserConfig(headless=False))
    agent = BrowserAgent(task=task_description, llm=_get_llm(), browser=browser, max_actions_per_step=5)
    result = await agent.run(max_steps=15)
    final = result.final_result() if hasattr(result, "final_result") else str(result)
    return final or "浏览器任务完成。"


@tool
def read_local_file(file_path: str) -> str:
    """读取本地文件内容。支持 .txt .md .json .csv 等文本格式。"""
    import os
    path = os.path.expanduser(file_path)
    if not os.path.exists(path):
        return f"文件不存在: {path}"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return content[:8000] + ("\n...(已截断)" if len(content) > 8000 else "")


@tool
def write_local_file(file_path: str, content: str) -> str:
    """将内容写入本地文件。文件不存在时自动创建。"""
    import os
    path = os.path.expanduser(file_path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入: {path} ({len(content)} 字符)"


TOOLS = [web_search, browser_navigate, read_local_file, write_local_file]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. LLM
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _get_llm():
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model="gpt-5.4",
        api_key="cr_751bd8df910f05852082e75fe02b04b2aecf475dba758891d3e97a553f8c993a",  # 你的 API Key
        base_url="https://co.yes.vg",  # OpenAI 官方；或替换为代理/兼容接口地址
    )
    # return ChatAnthropic(model="claude-sonnet-4-20250514", max_tokens=4096)
    # from langchain_openai import ChatOpenAI
    # return ChatOpenAI(model="gpt-4o")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. 节点
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SYSTEM_PROMPT = """你是一个在用户本地运行的智能 Agent。
可用工具：
- web_search: 快速搜索互联网
- browser_navigate: 控制浏览器完成复杂交互
- read_local_file: 读取本地文件
- write_local_file: 写入本地文件

策略：优先 web_search，复杂交互才用 browser_navigate，用户要求保存时主动调用 write_local_file。
"""

async def planner_node(state: AgentState) -> dict:
    """规划节点：LLM 决定下一步调用哪个工具，或直接给出答案。"""
    llm = _get_llm().bind_tools(TOOLS)
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
    response = await llm.ainvoke(messages)
    return {"messages": [response], "step_count": state["step_count"] + 1}

async def tool_executor_node(state: AgentState) -> dict:
    """工具执行节点：并发执行 planner 选择的工具。"""
    result = await ToolNode(TOOLS).ainvoke(state)
    return result

def finish_node(state: AgentState) -> dict:
    """结束节点：提取最终 AI 回答。"""
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            return {"final_result": str(msg.content)}
    return {"final_result": "任务完成。"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. 路由
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def should_continue(state: AgentState) -> Literal["tools", "finish"]:
    if state["step_count"] >= 20:
        return "finish"
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "finish"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Graph 构建
#
#   [START]
#      ↓
#   planner ←─────────────┐
#      ↓                  │
#   should_continue?      │
#    ├─ tools → executor ─┘  (循环)
#    └─ finish → [END]
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def build_graph():
    g = StateGraph(AgentState)
    g.add_node("planner", planner_node)
    g.add_node("tools", tool_executor_node)
    g.add_node("finish", finish_node)
    g.set_entry_point("planner")
    g.add_conditional_edges("planner", should_continue, {"tools": "tools", "finish": "finish"})
    g.add_edge("tools", "planner")
    g.add_edge("finish", END)
    return g.compile()

_GRAPH = None
def get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. 对外接口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def run_agent(task: str, on_step: Callable[[str], Awaitable[None]]) -> str:
    """
    执行 LangGraph Agent，通过 on_step 回调实时推送每步进度。
    main.py 接口完全不变。
    """
    graph = get_graph()
    initial_state: AgentState = {
        "messages": [HumanMessage(content=task)],
        "task": task,
        "step_count": 0,
        "final_result": "",
    }

    await on_step("LangGraph 初始化，开始规划任务...")

    async for event in graph.astream_events(initial_state, version="v2"):
        kind = event["event"]
        name = event.get("name", "")

        if kind == "on_chain_start" and name in ("planner", "tools", "finish"):
            label = {"planner": "🧠 规划中", "tools": "🔧 执行工具", "finish": "✅ 整理结果"}
            await on_step(f"{label[name]}...")

        elif kind == "on_tool_start":
            inp = event.get("data", {}).get("input", {})
            param = str(next(iter(inp.values()), "")) if isinstance(inp, dict) else str(inp)
            param = param[:80] + ("..." if len(param) > 80 else "")
            await on_step(f"调用工具 [{name}]: {param}")

        elif kind == "on_tool_end":
            out = str(event.get("data", {}).get("output", ""))
            summary = out[:120].replace("\n", " ") + ("..." if len(out) > 120 else "")
            await on_step(f"[{name}] 返回: {summary}")

        elif kind == "on_chat_model_start":
            await on_step("LLM 推理中...")

    final_state = await graph.ainvoke(initial_state)
    return final_state.get("final_result") or "任务完成。"
