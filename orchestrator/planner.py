"""
Orchestrator Planner — classifies user intent and generates execution plans.
1. Try to match a known template
2. If no match, use LLM free-form planning with full registry context
"""
import json

from langchain_core.messages import HumanMessage, SystemMessage

from models import get_llm
from registry import registry_summary
from orchestrator.templates import intent_descriptions, get_template, list_intents


CLASSIFY_SYSTEM = """你是一个任务分类器。根据用户的任务描述，判断最匹配的意图类型。

已知意图类型：
{intents}

输出 JSON 格式：
{{"intent": "意图名称", "confidence": 0.0-1.0, "params": {{"title": "...", "search_query": "...", "file_paths": [], "author": ""}}}}

如果没有匹配的意图（confidence < 0.5），输出：
{{"intent": "free_form", "confidence": 0.0, "params": {{...}}}}

只输出 JSON，不要其他内容。"""


FREEFORM_SYSTEM = """你是一个任务编排器。用户给了一个任务，不属于任何已知模板。
请根据可用的能力，生成一个执行计划。

可用能力：
{registry}

输出 JSON 格式：
{{
  "intent": "free_form",
  "title": "任务标题",
  "steps": [
    {{"id": 1, "type": "agent|tool|renderer", "name": "能力名称", "input_key": "上下文key", "depends_on": [], "input_description": "此步骤需要什么输入"}}
  ],
  "context": {{
    "输入key": "具体输入值"
  }}
}}

注意：
- depends_on 列出必须先完成的步骤 id
- 可以并行的步骤不要互相依赖
- input_key 是从 context 获取输入的 key
- 只输出 JSON"""


async def classify_and_plan(task: str) -> dict:
    """
    Classify user intent and return an execution plan.

    Returns:
        {
            "intent": str,
            "template": dict | None,
            "steps": list[dict],
            "context": dict,
        }
    """
    llm = get_llm("orchestrator")

    # Step 1: Classify intent
    classify_prompt = CLASSIFY_SYSTEM.format(intents=intent_descriptions())
    messages = [
        SystemMessage(content=classify_prompt),
        HumanMessage(content=task),
    ]
    response = await llm.ainvoke(messages)
    content = _extract_json(response.content)

    try:
        classification = json.loads(content)
    except json.JSONDecodeError:
        classification = {"intent": "free_form", "confidence": 0.0, "params": {}}

    intent = classification.get("intent", "free_form")
    confidence = classification.get("confidence", 0.0)
    params = classification.get("params", {})

    # Step 2: Match template or free-form plan
    template = get_template(intent) if confidence >= 0.5 else None

    if template:
        # Use template steps, populate context from params
        steps = template["steps"]
        context = {
            "search_query": params.get("search_query", task),
            "file_paths": params.get("file_paths", []),
            "title": params.get("title", task[:50]),
            "author": params.get("author", ""),
            "task": task,
            "storyline": "",
        }
        return {
            "intent": intent,
            "template": template,
            "steps": steps,
            "context": context,
        }

    # Step 3: Free-form LLM planning
    freeform_prompt = FREEFORM_SYSTEM.format(registry=registry_summary())
    messages = [
        SystemMessage(content=freeform_prompt),
        HumanMessage(content=task),
    ]
    response = await llm.ainvoke(messages)
    content = _extract_json(response.content)

    try:
        plan = json.loads(content)
    except json.JSONDecodeError:
        # Fallback: treat as quick question
        plan = {
            "intent": "quick_question",
            "steps": [{"id": 1, "type": "agent", "name": "general_chat", "input_key": "chat_input"}],
            "context": {"chat_input": task},
        }

    steps = plan.get("steps", [])
    context = plan.get("context", {"task": task})
    context["task"] = task

    return {
        "intent": plan.get("intent", "free_form"),
        "template": None,
        "steps": steps,
        "context": context,
    }


def _extract_json(text: str) -> str:
    """Extract JSON from LLM response, handling markdown code blocks."""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    return text.strip()
