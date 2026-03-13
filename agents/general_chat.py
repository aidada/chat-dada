"""
General Chat Agent — direct Q&A conversation, no tools needed.
Registered in registry as "general_chat".
"""
from langchain_core.messages import HumanMessage, SystemMessage
from models import get_llm


CHAT_SYSTEM = """你是一个专业、友好的AI助手。
- 直接回答用户问题，简洁准确
- 必要时提供结构化的要点
- 承认不确定的地方
- 使用中文回答"""


async def run(input_data) -> dict:
    """
    Unified interface: async def run(input, on_step) -> dict

    Args:
        input_data: str (question) or dict with "query" key
    """
    if isinstance(input_data, str):
        query = input_data
    elif isinstance(input_data, dict):
        query = input_data.get("query", input_data.get("chat_input", str(input_data)))
    else:
        query = str(input_data)

    llm = get_llm("orchestrator")
    messages = [
        SystemMessage(content=CHAT_SYSTEM),
        HumanMessage(content=query),
    ]
    response = await llm.ainvoke(messages)
    return {"status": "ok", "result": response.content}
