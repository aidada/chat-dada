"""
Translator Tool — single LLM call to translate text.
"""
from langchain_core.messages import HumanMessage, SystemMessage
from models import get_llm


async def run(input_data) -> dict:
    """
    Translate text to a target language.

    Args:
        input_data: dict with "text" and "target_lang" (default: "中文")
    """
    if isinstance(input_data, str):
        text = input_data
        target_lang = "中文"
    elif isinstance(input_data, dict):
        text = input_data.get("text", str(input_data))
        target_lang = input_data.get("target_lang", "中文")
    else:
        text = str(input_data)
        target_lang = "中文"

    llm = get_llm("writer")
    messages = [
        SystemMessage(content=f"你是一个专业翻译。将以下内容翻译为{target_lang}。只输出翻译结果，不要加任何解释。"),
        HumanMessage(content=text),
    ]
    response = await llm.ainvoke(messages)
    return {"status": "ok", "result": response.content}
