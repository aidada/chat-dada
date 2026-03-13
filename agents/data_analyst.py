"""
Data Analyst Agent — analyzes data files, generates insights, can execute code.
Uses doc_analyst tools + code_executor.
"""
from typing import Annotated, Literal
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from models import get_llm


@tool
def execute_python(code: str) -> str:
    """执行 Python 代码进行数据分析。可以使用 pandas, numpy 等常用库。"""
    import subprocess
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        tmp = f.name
    try:
        result = subprocess.run(
            ["python", tmp], capture_output=True, text=True, timeout=30
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Timeout (30s limit)"
    finally:
        os.unlink(tmp)


@tool
def read_data_file(file_path: str) -> str:
    """读取数据文件（CSV, JSON, Excel 等），返回前 50 行预览。"""
    import os
    path = os.path.expanduser(file_path)
    if not os.path.exists(path):
        return f"文件不存在: {path}"

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".csv":
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[:50]
            return "".join(lines)
        elif ext == ".json":
            import json
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return json.dumps(data, ensure_ascii=False, indent=2)[:5000]
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return content[:5000]
    except Exception as e:
        return f"读取失败: {e}"


ANALYST_TOOLS = [execute_python, read_data_file]


class AnalystState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    step_count: int
    analysis: str


ANALYST_SYSTEM = """你是一个专业的数据分析师。你的任务是分析数据并生成洞察。

策略：
1. 用 read_data_file 读取数据文件
2. 用 execute_python 编写分析代码（可用 pandas, numpy）
3. 总结关键发现、趋势、异常

输出格式：
- 数据概要（字段、行数、类型）
- 关键发现（趋势、分布、异常）
- 可视化建议（图表类型 + 数据列）"""


async def analyst_planner(state: AnalystState) -> dict:
    llm = get_llm("doc_analyst").bind_tools(ANALYST_TOOLS)
    messages = [SystemMessage(content=ANALYST_SYSTEM)] + state["messages"]
    response = await llm.ainvoke(messages)
    return {"messages": [response], "step_count": state["step_count"] + 1}


async def analyst_tools(state: AnalystState) -> dict:
    return await ToolNode(ANALYST_TOOLS).ainvoke(state)


def analyst_finish(state: AnalystState) -> dict:
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            return {"analysis": str(msg.content)}
    return {"analysis": ""}


def analyst_should_continue(state: AnalystState) -> Literal["tools", "finish"]:
    if state["step_count"] >= 10:
        return "finish"
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "finish"


def build_analyst_graph():
    g = StateGraph(AnalystState)
    g.add_node("planner", analyst_planner)
    g.add_node("tools", analyst_tools)
    g.add_node("finish", analyst_finish)
    g.set_entry_point("planner")
    g.add_conditional_edges("planner", analyst_should_continue, {"tools": "tools", "finish": "finish"})
    g.add_edge("tools", "planner")
    g.add_edge("finish", END)
    return g.compile()


async def run(input_data) -> dict:
    """Unified interface for registry dispatch."""
    if isinstance(input_data, str):
        query = input_data
    elif isinstance(input_data, dict):
        query = input_data.get("query", input_data.get("analysis_input", str(input_data)))
    else:
        query = str(input_data)

    graph = build_analyst_graph()
    state = {
        "messages": [HumanMessage(content=f"请分析以下数据/需求：\n{query}")],
        "step_count": 0,
        "analysis": "",
    }
    result = await graph.ainvoke(state)
    return {"status": "ok", "result": result.get("analysis", "")}
