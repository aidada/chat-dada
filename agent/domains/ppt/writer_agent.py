"""
Writer Agent — takes storyline + gathered materials and produces Slide DSL JSON.
Uses the "writer" role LLM.
"""
import json

from langchain_core.messages import HumanMessage, SystemMessage

from core.models import get_llm, response_text
from core.logger import log_async
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


@log_async("agent", "writer_agent")
async def run_writer(
    storyline: str | dict,
    search_findings: str = "",
    doc_analysis: str = "",
    author: str = "",
) -> SlideDeck:
    """Run the writer agent to produce Slide DSL from materials."""
    if isinstance(storyline, dict):
        input_data = storyline
        storyline = str(input_data.get("storyline", ""))
        search_findings = str(input_data.get("search_findings", search_findings))
        doc_analysis = str(input_data.get("doc_analysis", doc_analysis))
        author = str(input_data.get("author", author))

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
    content = response_text(response)

    # Extract JSON from response (handle markdown code blocks)
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        content = content.split("```")[1].split("```")[0]
    content = content.strip()

    data = json.loads(content)
    return SlideDeck.model_validate(data)
