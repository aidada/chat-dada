# Multi-Agent PPT Generation System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Transform single-agent LangGraph system into a multi-agent, multi-model architecture that can research topics, analyze documents, and generate editable PPT files.

**Architecture:** Orchestrator dispatches concurrent sub-agents (Search, Doc Analyst) to gather materials, then Writer agent produces Slide DSL JSON, which PPT Engine renders to `.pptx` via python-pptx. Each agent uses independently configured LLM instances.

**Tech Stack:** LangGraph (StateGraph), langchain-openai (ChatOpenAI), python-pptx, pypdf, pydantic, FastAPI + WebSocket

---

## Task 1: Install Dependencies & Create Directory Structure

**Files:**
- Modify: `requirements.txt`
- Create: `agents/__init__.py`, `ppt_engine/__init__.py`, `outputs/.gitkeep`

**Step 1: Install python-pptx**

Run:
```bash
/Users/luozhongxu/workspace/chat-dada/.venv/bin/python -m pip install python-pptx
```

**Step 2: Update requirements.txt**

Add to end of `requirements.txt`:

```
# Multi-model support
langchain-openai>=0.1.0

# PPT generation
python-pptx>=0.6.23

# PDF parsing (already installed)
pypdf>=4.0.0
```

**Step 3: Create directories**

Run:
```bash
mkdir -p agents ppt_engine outputs
touch agents/__init__.py ppt_engine/__init__.py outputs/.gitkeep
```

**Step 4: Commit**

```bash
git add requirements.txt agents/ ppt_engine/ outputs/
git commit -m "chore: add directory structure and dependencies for multi-agent system"
```

---

## Task 2: Model Registry (`models.py`)

**Files:**
- Create: `models.py`

**Step 1: Create models.py**

```python
"""
Model registry — centralized LLM configuration for all agents.
Each agent role gets its own model config. Change models per-role here.
"""
from langchain_openai import ChatOpenAI


# All model configs in one place. Swap models per role as needed.
MODEL_CONFIGS = {
    "orchestrator": {
        "model": "gpt-5.4",
        "base_url": "https://co.yes.vg",
        "api_key": "cr_751bd8df910f05852082e75fe02b04b2aecf475dba758891d3e97a553f8c993a",
    },
    "search": {
        "model": "gpt-5.4",
        "base_url": "https://co.yes.vg",
        "api_key": "cr_751bd8df910f05852082e75fe02b04b2aecf475dba758891d3e97a553f8c993a",
    },
    "doc_analyst": {
        "model": "gpt-5.4",
        "base_url": "https://co.yes.vg",
        "api_key": "cr_751bd8df910f05852082e75fe02b04b2aecf475dba758891d3e97a553f8c993a",
    },
    "writer": {
        "model": "gpt-5.4",
        "base_url": "https://co.yes.vg",
        "api_key": "cr_751bd8df910f05852082e75fe02b04b2aecf475dba758891d3e97a553f8c993a",
    },
}


def get_llm(role: str, **kwargs) -> ChatOpenAI:
    """Get an LLM instance for a specific agent role.

    Args:
        role: One of "orchestrator", "search", "doc_analyst", "writer"
        **kwargs: Override any config value (e.g. max_tokens=8192)
    """
    config = MODEL_CONFIGS[role].copy()
    config.update(kwargs)
    return ChatOpenAI(
        model=config.pop("model"),
        api_key=config.pop("api_key"),
        base_url=config.pop("base_url"),
        **config,
    )
```

**Step 2: Verify import works**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "from models import get_llm; llm = get_llm('orchestrator'); print(type(llm))"
```
Expected: `<class 'langchain_openai.chat_models.base.ChatOpenAI'>`

**Step 3: Commit**

```bash
git add models.py
git commit -m "feat: add model registry with per-role LLM config"
```

---

## Task 3: Slide DSL Schema (`ppt_engine/dsl_schema.py`)

**Files:**
- Create: `ppt_engine/dsl_schema.py`

**Step 1: Create the Pydantic schema**

```python
"""
Slide DSL — structured JSON schema for PPT content.
LLM agents output this schema. PPT Engine consumes it.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class ChartData(BaseModel):
    type: str = Field(description="Chart type: bar, line, pie, radar")
    data: dict = Field(description='{"labels": [...], "values": [...]} or {"labels": [...], "series": [{"name": "...", "values": [...]}]}')
    unit: Optional[str] = None


class Slide(BaseModel):
    layout: str = Field(description="One of: title_slide, section_header, content_only, content_with_chart, content_with_image, two_column, comparison, summary")
    title: str = ""
    subtitle: Optional[str] = None
    body: Optional[str] = None
    body_left: Optional[str] = None
    body_right: Optional[str] = None
    chart: Optional[ChartData] = None
    image_prompt: Optional[str] = None
    speaker_notes: Optional[str] = None


class SlideDeck(BaseModel):
    meta: DeckMeta
    slides: list[Slide]


class DeckMeta(BaseModel):
    title: str
    author: str = ""
    theme: str = "academic_blue"


# Fix forward reference
SlideDeck.model_rebuild()
```

**Step 2: Verify schema loads**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from ppt_engine.dsl_schema import SlideDeck
deck = SlideDeck.model_validate({
    'meta': {'title': 'Test', 'author': 'Me'},
    'slides': [{'layout': 'title_slide', 'title': 'Hello', 'subtitle': 'World'}]
})
print(deck.model_dump_json(indent=2))
"
```
Expected: valid JSON output with meta and slides.

**Step 3: Commit**

```bash
git add ppt_engine/
git commit -m "feat: add Slide DSL Pydantic schema"
```

---

## Task 4: PPT Renderer (`ppt_engine/renderer.py`)

**Files:**
- Create: `ppt_engine/renderer.py`

**Step 1: Create renderer**

```python
"""
PPT Engine — renders SlideDeck JSON to editable .pptx files via python-pptx.
"""
import os
from pathlib import Path
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.enum.text import PP_ALIGN
from pptx.enum.chart import XL_CHART_TYPE
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor

from ppt_engine.dsl_schema import SlideDeck, Slide, ChartData


# Theme color palettes
THEMES = {
    "academic_blue": {
        "title_bg": RGBColor(0x00, 0x3C, 0x71),
        "title_fg": RGBColor(0xFF, 0xFF, 0xFF),
        "body_fg": RGBColor(0x33, 0x33, 0x33),
        "accent": RGBColor(0x00, 0x7B, 0xC0),
    },
    "business_gray": {
        "title_bg": RGBColor(0x2D, 0x2D, 0x2D),
        "title_fg": RGBColor(0xFF, 0xFF, 0xFF),
        "body_fg": RGBColor(0x33, 0x33, 0x33),
        "accent": RGBColor(0xE8, 0x4E, 0x0E),
    },
}

CHART_TYPE_MAP = {
    "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "line": XL_CHART_TYPE.LINE,
    "pie": XL_CHART_TYPE.PIE,
}


def render_pptx(deck: SlideDeck, output_path: str) -> str:
    """Render a SlideDeck to a .pptx file. Returns the output file path."""
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    theme = THEMES.get(deck.meta.theme, THEMES["academic_blue"])

    for slide_data in deck.slides:
        _add_slide(prs, slide_data, theme)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    prs.save(output_path)
    return output_path


def _add_slide(prs: Presentation, s: Slide, theme: dict):
    """Add a single slide based on layout type."""
    slide_layout = prs.slide_layouts[6]  # blank layout
    slide = prs.slides.add_slide(slide_layout)

    if s.layout == "title_slide":
        _render_title_slide(slide, s, theme)
    elif s.layout == "section_header":
        _render_section_header(slide, s, theme)
    elif s.layout == "content_with_chart":
        _render_content_with_chart(slide, s, theme)
    elif s.layout == "two_column":
        _render_two_column(slide, s, theme)
    elif s.layout == "comparison":
        _render_two_column(slide, s, theme)  # same layout, different label
    else:
        # content_only, content_with_image, summary — all text-based
        _render_content_only(slide, s, theme)

    # Speaker notes
    if s.speaker_notes:
        slide.notes_slide.notes_text_frame.text = s.speaker_notes


def _render_title_slide(slide, s: Slide, theme: dict):
    # Full background color
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = theme["title_bg"]

    # Title
    txBox = slide.shapes.add_textbox(Inches(1), Inches(2.2), Inches(11), Inches(2))
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = s.title
    p.font.size = Pt(44)
    p.font.bold = True
    p.font.color.rgb = theme["title_fg"]
    p.alignment = PP_ALIGN.CENTER

    # Subtitle
    if s.subtitle:
        txBox2 = slide.shapes.add_textbox(Inches(1), Inches(4.5), Inches(11), Inches(1))
        tf2 = txBox2.text_frame
        tf2.word_wrap = True
        p2 = tf2.paragraphs[0]
        p2.text = s.subtitle
        p2.font.size = Pt(24)
        p2.font.color.rgb = theme["title_fg"]
        p2.alignment = PP_ALIGN.CENTER


def _render_section_header(slide, s: Slide, theme: dict):
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = theme["accent"]

    txBox = slide.shapes.add_textbox(Inches(1), Inches(2.8), Inches(11), Inches(2))
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = s.title
    p.font.size = Pt(40)
    p.font.bold = True
    p.font.color.rgb = theme["title_fg"]
    p.alignment = PP_ALIGN.CENTER


def _render_content_only(slide, s: Slide, theme: dict):
    # Title bar
    txBox = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(12), Inches(1))
    tf = txBox.text_frame
    p = tf.paragraphs[0]
    p.text = s.title
    p.font.size = Pt(32)
    p.font.bold = True
    p.font.color.rgb = theme["accent"]

    # Body
    if s.body:
        txBox2 = slide.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(12), Inches(5.5))
        tf2 = txBox2.text_frame
        tf2.word_wrap = True
        for i, line in enumerate(s.body.split("\n")):
            if i == 0:
                tf2.paragraphs[0].text = line
                tf2.paragraphs[0].font.size = Pt(18)
                tf2.paragraphs[0].font.color.rgb = theme["body_fg"]
            else:
                p = tf2.add_paragraph()
                p.text = line
                p.font.size = Pt(18)
                p.font.color.rgb = theme["body_fg"]

    # Image prompt placeholder
    if s.image_prompt:
        txBox3 = slide.shapes.add_textbox(Inches(8), Inches(2), Inches(4.5), Inches(4))
        tf3 = txBox3.text_frame
        tf3.word_wrap = True
        tf3.paragraphs[0].text = f"[Image: {s.image_prompt}]"
        tf3.paragraphs[0].font.size = Pt(12)
        tf3.paragraphs[0].font.italic = True
        tf3.paragraphs[0].font.color.rgb = RGBColor(0x99, 0x99, 0x99)


def _render_content_with_chart(slide, s: Slide, theme: dict):
    # Title
    txBox = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(12), Inches(1))
    tf = txBox.text_frame
    p = tf.paragraphs[0]
    p.text = s.title
    p.font.size = Pt(32)
    p.font.bold = True
    p.font.color.rgb = theme["accent"]

    # Body text (left side)
    if s.body:
        txBox2 = slide.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(5.5), Inches(5.5))
        tf2 = txBox2.text_frame
        tf2.word_wrap = True
        for i, line in enumerate(s.body.split("\n")):
            if i == 0:
                tf2.paragraphs[0].text = line
                tf2.paragraphs[0].font.size = Pt(16)
                tf2.paragraphs[0].font.color.rgb = theme["body_fg"]
            else:
                p2 = tf2.add_paragraph()
                p2.text = line
                p2.font.size = Pt(16)
                p2.font.color.rgb = theme["body_fg"]

    # Chart (right side)
    if s.chart:
        _add_chart(slide, s.chart, Inches(6.5), Inches(1.5), Inches(6), Inches(5))


def _render_two_column(slide, s: Slide, theme: dict):
    # Title
    txBox = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(12), Inches(1))
    tf = txBox.text_frame
    p = tf.paragraphs[0]
    p.text = s.title
    p.font.size = Pt(32)
    p.font.bold = True
    p.font.color.rgb = theme["accent"]

    # Left column
    left_text = s.body_left or s.body or ""
    txBox_l = slide.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(5.8), Inches(5.5))
    tf_l = txBox_l.text_frame
    tf_l.word_wrap = True
    for i, line in enumerate(left_text.split("\n")):
        if i == 0:
            tf_l.paragraphs[0].text = line
            tf_l.paragraphs[0].font.size = Pt(16)
        else:
            p = tf_l.add_paragraph()
            p.text = line
            p.font.size = Pt(16)

    # Right column
    right_text = s.body_right or ""
    txBox_r = slide.shapes.add_textbox(Inches(6.8), Inches(1.5), Inches(5.8), Inches(5.5))
    tf_r = txBox_r.text_frame
    tf_r.word_wrap = True
    for i, line in enumerate(right_text.split("\n")):
        if i == 0:
            tf_r.paragraphs[0].text = line
            tf_r.paragraphs[0].font.size = Pt(16)
        else:
            p = tf_r.add_paragraph()
            p.text = line
            p.font.size = Pt(16)


def _add_chart(slide, chart: ChartData, left, top, width, height):
    """Add a chart shape to the slide."""
    chart_type = CHART_TYPE_MAP.get(chart.type, XL_CHART_TYPE.COLUMN_CLUSTERED)
    cd = CategoryChartData()

    labels = chart.data.get("labels", [])
    cd.categories = labels

    # Support single series (values) or multi-series (series)
    if "values" in chart.data:
        series_name = chart.unit or "Value"
        cd.add_series(series_name, chart.data["values"])
    elif "series" in chart.data:
        for s in chart.data["series"]:
            cd.add_series(s["name"], s["values"])

    slide.shapes.add_chart(chart_type, left, top, width, height, cd)
```

**Step 2: Quick smoke test**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from ppt_engine.dsl_schema import SlideDeck
from ppt_engine.renderer import render_pptx

deck = SlideDeck.model_validate({
    'meta': {'title': 'Test Deck', 'author': 'Test'},
    'slides': [
        {'layout': 'title_slide', 'title': 'Hello World', 'subtitle': 'Smoke Test'},
        {'layout': 'content_only', 'title': 'Page 2', 'body': 'Line 1\nLine 2\nLine 3'},
        {'layout': 'content_with_chart', 'title': 'Chart Slide', 'body': 'Some data analysis', 'chart': {'type': 'bar', 'data': {'labels': ['A','B','C'], 'values': [10,20,30]}, 'unit': 'GW'}},
    ]
})
path = render_pptx(deck, 'outputs/smoke_test.pptx')
print(f'OK: {path}')
"
```
Expected: `OK: outputs/smoke_test.pptx`

**Step 3: Commit**

```bash
git add ppt_engine/
git commit -m "feat: add PPT renderer with theme support and chart rendering"
```

---

## Task 5: Search Agent (`agents/search_agent.py`)

**Files:**
- Create: `agents/search_agent.py`

**Step 1: Create the search agent sub-graph**

```python
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
from browser_use.browser.browser import Browser, BrowserConfig


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


SEARCH_TOOLS = [web_search, browser_navigate]


# ── Nodes ──
SEARCH_SYSTEM = """你是一个专业的搜索研究员。你的任务是根据给定的搜索主题，通过多次搜索收集全面的信息。

策略：
1. 先用 web_search 进行关键词搜索
2. 如果需要抓取具体网页内容，用 browser_navigate
3. 收集足够信息后，直接用文字总结你的发现

输出格式：将所有发现整理为结构化的要点，包含来源 URL。"""


async def search_planner(state: SearchState) -> dict:
    llm = get_llm("search").bind_tools(SEARCH_TOOLS)
    messages = [SystemMessage(content=SEARCH_SYSTEM)] + state["messages"]
    response = await llm.ainvoke(messages)
    return {"messages": [response], "step_count": state["step_count"] + 1}


async def search_tool_executor(state: SearchState) -> dict:
    return await ToolNode(SEARCH_TOOLS).ainvoke(state)


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
    g = StateGraph(SearchState)
    g.add_node("planner", search_planner)
    g.add_node("tools", search_tool_executor)
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
```

**Step 2: Verify import**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "from agents.search_agent import build_search_graph; g = build_search_graph(); print('search graph OK')"
```
Expected: `search graph OK`

**Step 3: Commit**

```bash
git add agents/
git commit -m "feat: add search agent sub-graph with web_search and browser tools"
```

---

## Task 6: Document Analyst Agent (`agents/doc_agent.py`)

**Files:**
- Create: `agents/doc_agent.py`

**Step 1: Create the doc analysis agent**

```python
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
```

**Step 2: Verify import**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "from agents.doc_agent import build_doc_graph; g = build_doc_graph(); print('doc graph OK')"
```
Expected: `doc graph OK`

**Step 3: Commit**

```bash
git add agents/
git commit -m "feat: add document analyst agent with PDF and text parsing"
```

---

## Task 7: Writer Agent (`agents/writer_agent.py`)

**Files:**
- Create: `agents/writer_agent.py`

**Step 1: Create the writer agent**

```python
"""
Writer Agent — takes storyline + gathered materials and produces Slide DSL JSON.
Uses the "writer" role LLM.
"""
import json

from langchain_core.messages import HumanMessage, SystemMessage

from models import get_llm
from ppt_engine.dsl_schema import SlideDeck


WRITER_SYSTEM = """你是一个专业的 PPT 内容撰写专家。你会收到：
1. PPT 大纲/Storyline
2. 搜索结果和文档分析素材

你的任务是输出一个严格符合 JSON schema 的 Slide DSL。

输出要求 — 你必须输出合法的 JSON，结构如下：
{
  "meta": {"title": "...", "author": "...", "theme": "academic_blue"},
  "slides": [
    {"layout": "title_slide", "title": "...", "subtitle": "..."},
    {"layout": "content_only", "title": "...", "body": "...", "speaker_notes": "..."},
    ...
  ]
}

可用的 layout 类型：
- title_slide: 封面（title + subtitle）
- section_header: 章节分隔页（title）
- content_only: 纯文字（title + body + speaker_notes）
- content_with_chart: 文字+图表（title + body + chart + speaker_notes）
  chart 格式: {"type": "bar|line|pie", "data": {"labels": [...], "values": [...]}, "unit": "..."}
- content_with_image: 文字+图片占位（title + body + image_prompt + speaker_notes）
- two_column: 左右分栏（title + body_left + body_right + speaker_notes）
- comparison: 对比页（同 two_column）
- summary: 总结页（title + body + speaker_notes）

写作原则：
- 每页正文控制在 50-80 字，要点化表达
- speaker_notes 写详细讲解内容（100-200 字）
- 图表用真实数据（从素材中提取）
- 学术报告风格：严谨、有数据支撑
- 只输出 JSON，不要输出其他内容"""


async def run_writer(storyline: str, search_findings: str, doc_analysis: str, author: str = "") -> SlideDeck:
    """Run the writer agent to produce Slide DSL from materials."""
    llm = get_llm("writer")
    prompt = f"""## PPT 大纲
{storyline}

## 搜索研究结果
{search_findings}

## 文档分析结果
{doc_analysis}

请根据以上材料，生成完整的 Slide DSL JSON。作者: {author}"""

    messages = [
        SystemMessage(content=WRITER_SYSTEM),
        HumanMessage(content=prompt),
    ]
    response = await llm.ainvoke(messages)
    content = response.content

    # Extract JSON from response (handle markdown code blocks)
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        content = content.split("```")[1].split("```")[0]
    content = content.strip()

    data = json.loads(content)
    return SlideDeck.model_validate(data)
```

**Step 2: Verify import**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "from agents.writer_agent import run_writer; print('writer OK')"
```
Expected: `writer OK`

**Step 3: Commit**

```bash
git add agents/
git commit -m "feat: add writer agent that produces Slide DSL JSON from materials"
```

---

## Task 8: Orchestrator Agent (`agents/orchestrator.py`)

**Files:**
- Create: `agents/orchestrator.py`

**Step 1: Create the orchestrator — main graph that coordinates all agents**

```python
"""
Orchestrator Agent — main graph that:
1. Understands the task and generates a storyline
2. Dispatches Search + Doc agents in parallel
3. Runs Writer to produce Slide DSL
4. Renders PPT via PPT Engine

Exposes run_agent() with the same callback interface as the old agent.py.
"""
import asyncio
import json
import uuid
from typing import Callable, Awaitable

from langchain_core.messages import HumanMessage, SystemMessage

from models import get_llm
from agents.search_agent import run_search
from agents.doc_agent import run_doc_analysis
from agents.writer_agent import run_writer
from ppt_engine.renderer import render_pptx


ORCHESTRATOR_SYSTEM = """你是一个任务编排 Agent。用户会给你一个研究或报告任务。

你需要输出一个 JSON 执行计划，格式如下：
{
  "storyline": "PPT 大纲，用 \\n 分隔每个章节标题",
  "search_queries": ["搜索关键词1", "搜索关键词2", ...],
  "file_paths": ["如有本地文件路径写在这里"],
  "title": "PPT 标题",
  "author": "作者（如用户未提供则留空）"
}

注意：
- search_queries: 为搜索 Agent 提供 2-5 个搜索关键词
- file_paths: 从用户消息中提取文件路径（如有）
- storyline: 规划 PPT 的叙事结构，每行一个章节
- 只输出 JSON，不要其他内容"""


async def run_agent(task: str, on_step: Callable[[str], Awaitable[None]]) -> str:
    """
    Main entry point — same interface as the old agent.py run_agent().
    Returns final result text. Sends file info via on_step callback.
    """
    await on_step("🧠 Orchestrator: 分析任务，规划执行计划...")

    # Step 1: Orchestrator plans the task
    llm = get_llm("orchestrator")
    messages = [
        SystemMessage(content=ORCHESTRATOR_SYSTEM),
        HumanMessage(content=task),
    ]
    response = await llm.ainvoke(messages)
    content = response.content

    # Parse plan JSON
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        content = content.split("```")[1].split("```")[0]
    content = content.strip()

    try:
        plan = json.loads(content)
    except json.JSONDecodeError:
        await on_step("⚠️ 执行计划解析失败，使用默认计划...")
        plan = {
            "storyline": f"背景介绍\n核心内容\n数据分析\n总结展望",
            "search_queries": [task],
            "file_paths": [],
            "title": task[:30],
            "author": "",
        }

    storyline = plan.get("storyline", "")
    search_queries = plan.get("search_queries", [])
    file_paths = plan.get("file_paths", [])
    title = plan.get("title", "Report")
    author = plan.get("author", "")

    await on_step(f"📋 Storyline:\n{storyline}")

    # Step 2: Dispatch Search + Doc agents in parallel
    tasks = []

    # Search tasks
    if search_queries:
        await on_step(f"🔍 Search Agent: 开始搜索 {len(search_queries)} 个主题...")
        combined_query = "\n".join(f"- {q}" for q in search_queries)
        tasks.append(("search", run_search(combined_query)))

    # Doc analysis tasks
    if file_paths:
        await on_step(f"📄 Doc Agent: 开始分析 {len(file_paths)} 个文件...")
        tasks.append(("doc", run_doc_analysis(file_paths)))

    # Run concurrently
    search_findings = ""
    doc_analysis = ""

    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
        for (label, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                await on_step(f"⚠️ {label} Agent 出错: {result}")
                continue
            if label == "search":
                search_findings = result
                await on_step(f"🔍 Search Agent: 搜索完成，收集到 {len(result)} 字素材")
            elif label == "doc":
                doc_analysis = result
                await on_step(f"📄 Doc Agent: 文档分析完成，提取 {len(result)} 字要点")
    else:
        await on_step("ℹ️ 无搜索/文档任务，直接进入内容生成...")

    # Step 3: Writer produces Slide DSL
    await on_step("✍️ Writer Agent: 正在生成 PPT 内容...")
    try:
        deck = await run_writer(storyline, search_findings, doc_analysis, author)
        # Override title from plan
        deck.meta.title = title
        if author:
            deck.meta.author = author
        await on_step(f"✍️ Writer Agent: 完成，共 {len(deck.slides)} 页 Slide")
    except Exception as e:
        await on_step(f"⚠️ Writer 生成失败: {e}")
        return f"PPT 内容生成失败: {e}"

    # Step 4: Render to .pptx
    await on_step("📊 PPT Engine: 正在渲染 .pptx 文件...")
    file_id = uuid.uuid4().hex[:8]
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip()[:30] or "report"
    filename = f"{safe_title}_{file_id}.pptx"
    output_path = f"outputs/{filename}"

    try:
        render_pptx(deck, output_path)
        await on_step(f"✅ PPT 已生成: {filename}")
        # Send file download message
        await on_step(json.dumps({
            "type": "file",
            "url": f"/download/{filename}",
            "name": filename,
        }))
    except Exception as e:
        await on_step(f"⚠️ PPT 渲染失败: {e}")
        return f"PPT 渲染失败: {e}"

    return f"PPT 已生成完成：《{title}》，共 {len(deck.slides)} 页。\n下载链接: /download/{filename}"
```

**Step 2: Verify import chain**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "from agents.orchestrator import run_agent; print('orchestrator OK')"
```
Expected: `orchestrator OK`

**Step 3: Commit**

```bash
git add agents/
git commit -m "feat: add orchestrator agent with concurrent dispatch and PPT generation"
```

---

## Task 9: Update `main.py` — Wire New Architecture + Download Endpoint

**Files:**
- Modify: `main.py:12` (change import)
- Modify: `main.py` (add download route, must be before static mount)

**Step 1: Update main.py**

Replace the full `main.py` with:

```python
"""
Local Agent - FastAPI + Multi-Agent + WebSocket 实时推送
运行: uvicorn main:app --reload --port 8000
"""
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from agents.orchestrator import run_agent

app = FastAPI(title="Local Agent")


@app.get("/")
async def index():
    """返回前端页面"""
    html = Path("static/index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/download/{filename}")
async def download_file(filename: str):
    """下载生成的文件（PPT 等）"""
    # Sanitize filename to prevent path traversal
    safe_name = Path(filename).name
    path = Path("outputs") / safe_name
    if not path.exists():
        return {"error": "文件不存在"}
    return FileResponse(path, filename=safe_name)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket 端点：接收任务 → 执行 Agent → 实时推送步骤
    消息格式:
      Client → Server: {"task": "帮我搜索..."}
      Server → Client: {"type": "step"|"result"|"error"|"file", "content": "..."}
    """
    await websocket.accept()
    print("[WS] 客户端连接")

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            task = data.get("task", "").strip()

            if not task:
                await websocket.send_json({"type": "error", "content": "任务不能为空"})
                continue

            print(f"[Task] {task}")
            await websocket.send_json({"type": "start", "content": f"开始执行: {task}"})

            async def on_step(step_info: str):
                # Check if step_info is a JSON file message
                try:
                    parsed = json.loads(step_info)
                    if isinstance(parsed, dict) and parsed.get("type") == "file":
                        await websocket.send_json(parsed)
                        return
                except (json.JSONDecodeError, TypeError):
                    pass
                await websocket.send_json({"type": "step", "content": step_info})

            try:
                result = await run_agent(task, on_step)
                await websocket.send_json({"type": "result", "content": result})
            except Exception as e:
                await websocket.send_json({"type": "error", "content": str(e)})

    except WebSocketDisconnect:
        print("[WS] 客户端断开")


# Mount static files LAST (after API routes)
app.mount("/static", StaticFiles(directory="static"), name="static")
```

**Step 2: Verify server starts**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && timeout 5 .venv/bin/python -c "from main import app; print('main.py OK')" 2>&1 || true
```
Expected: `main.py OK` (may fail if static/ doesn't exist yet, which is fine)

**Step 3: Commit**

```bash
git add main.py
git commit -m "feat: wire multi-agent orchestrator into main.py with download endpoint"
```

---

## Task 10: Update `requirements.txt` and Final Integration Test

**Files:**
- Modify: `requirements.txt`

**Step 1: Finalize requirements.txt**

```
# 服务框架
fastapi>=0.110.0
uvicorn[standard]>=0.29.0
websockets>=12.0
python-dotenv>=1.0.0

# LangGraph + LangChain
langgraph>=0.2.0
langchain-core>=0.2.0
langchain-openai>=0.1.0
langchain-community>=0.2.0

# 浏览器控制
browser-use>=0.1.40
playwright>=1.44.0

# 搜索（可选）
tavily-python>=0.3.0

# 文档处理
pypdf>=4.0.0

# PPT 生成
python-pptx>=0.6.23
```

**Step 2: Full import chain test**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from models import get_llm
from agents.search_agent import build_search_graph
from agents.doc_agent import build_doc_graph
from agents.writer_agent import run_writer
from agents.orchestrator import run_agent
from ppt_engine.dsl_schema import SlideDeck
from ppt_engine.renderer import render_pptx
print('All modules imported OK')
print('Models:', list(get_llm.__code__.co_varnames))
print('Search graph nodes:', list(build_search_graph().get_graph().nodes))
print('Doc graph nodes:', list(build_doc_graph().get_graph().nodes))
"
```
Expected: `All modules imported OK` + graph node listings

**Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore: finalize requirements with all multi-agent dependencies"
```

---

## Summary

| Task | Component | Files |
|------|-----------|-------|
| 1 | Dependencies & dirs | `requirements.txt`, `agents/`, `ppt_engine/`, `outputs/` |
| 2 | Model registry | `models.py` |
| 3 | Slide DSL schema | `ppt_engine/dsl_schema.py` |
| 4 | PPT renderer | `ppt_engine/renderer.py` |
| 5 | Search agent | `agents/search_agent.py` |
| 6 | Doc analyst agent | `agents/doc_agent.py` |
| 7 | Writer agent | `agents/writer_agent.py` |
| 8 | Orchestrator | `agents/orchestrator.py` |
| 9 | Main.py rewire | `main.py` |
| 10 | Final integration | `requirements.txt` |

The old `agent.py` is kept as-is for reference/fallback. The new entry point is `agents/orchestrator.py` which exports `run_agent()` with the same callback signature.
