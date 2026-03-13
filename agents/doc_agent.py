"""
Document Analyst Agent — reads PDF/text files and extracts structured information.
Uses its own LLM instance (configured as "doc_analyst" role).
"""
import os
from typing import Annotated, Literal
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from models import get_llm


# ── Tools ──
@tool
def read_text_file(file_path: str) -> str:
    """读取文本文件内容（.txt .md .json .csv 等）。"""
    path = os.path.expanduser(file_path)
    if not os.path.exists(path):
        return f"文件不存在: {path}"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return content[:15000] + ("\n...(已截断)" if len(content) > 15000 else "")


@tool
def read_pdf_file(file_path: str) -> str:
    """读取 PDF 文件并提取文本内容。"""
    path = os.path.expanduser(file_path)
    if not os.path.exists(path):
        return f"文件不存在: {path}"
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(f"--- Page {i+1} ---\n{text}")
        content = "\n".join(pages)
        return content[:15000] + ("\n...(已截断)" if len(content) > 15000 else "")
    except Exception as e:
        return f"PDF 解析失败: {e}"


DOC_TOOLS = [read_text_file, read_pdf_file]


# ── State ──
class DocState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    file_paths: list[str]
    step_count: int
    analysis: str


# ── Nodes ──
DOC_SYSTEM = """你是一个专业的文档分析师。你的任务是读取给定的文件，提取关键信息。

策略：
1. 逐个读取给定的文件（PDF 用 read_pdf_file，其他用 read_text_file）
2. 提取关键论点、数据、结论
3. 整理为结构化要点

输出格式：
- 每个文件的核心要点（带引用）
- 关键数据和图表描述
- 主要结论和发现"""


async def doc_planner(state: DocState) -> dict:
    llm = get_llm("doc_analyst").bind_tools(DOC_TOOLS)
    messages = [SystemMessage(content=DOC_SYSTEM)] + state["messages"]
    response = await llm.ainvoke(messages)
    return {"messages": [response], "step_count": state["step_count"] + 1}


async def doc_tool_executor(state: DocState) -> dict:
    return await ToolNode(DOC_TOOLS).ainvoke(state)


def doc_finish(state: DocState) -> dict:
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            return {"analysis": str(msg.content)}
    return {"analysis": ""}


def doc_should_continue(state: DocState) -> Literal["tools", "finish"]:
    if state["step_count"] >= 10:
        return "finish"
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "finish"


# ── Graph ──
def build_doc_graph():
    g = StateGraph(DocState)
    g.add_node("planner", doc_planner)
    g.add_node("tools", doc_tool_executor)
    g.add_node("finish", doc_finish)
    g.set_entry_point("planner")
    g.add_conditional_edges("planner", doc_should_continue, {"tools": "tools", "finish": "finish"})
    g.add_edge("tools", "planner")
    g.add_edge("finish", END)
    return g.compile()


async def run_doc_analysis(file_paths: list[str]) -> str:
    """Run the doc analyst agent on a list of files."""
    graph = build_doc_graph()
    files_desc = "\n".join(f"- {p}" for p in file_paths)
    state = {
        "messages": [HumanMessage(content=f"请分析以下文件并提取关键信息：\n{files_desc}")],
        "file_paths": file_paths,
        "step_count": 0,
        "analysis": "",
    }
    result = await graph.ainvoke(state)
    return result.get("analysis", "")
