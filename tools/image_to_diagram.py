"""
Image-to-Diagram Tool — uses a vision model to convert images into structured diagram JSON.
Accepts an image path, sends it to a vision-capable LLM, and returns a structured description.
"""
import os
import base64
import logging

from langchain_core.messages import HumanMessage, SystemMessage
from core.models import get_llm, response_text
from core.logger import log_async
from core import r2_storage

log = logging.getLogger("chatdada.image_to_diagram")


DIAGRAM_SYSTEM = """你是一个专业的图表分析师。分析给定的图片，将其转换为结构化的图表 JSON 描述。

输出 JSON 格式：
{
  "type": "flowchart|org_chart|sequence|architecture|mindmap|other",
  "title": "图表标题",
  "nodes": [
    {"id": "n1", "label": "节点文本", "shape": "rect|circle|diamond|ellipse", "style": ""}
  ],
  "edges": [
    {"from": "n1", "to": "n2", "label": "连接文本", "style": "solid|dashed|dotted"}
  ],
  "description": "图表的文字描述和解读"
}

注意：
- 尽可能准确地识别图中的所有节点和连接关系
- shape 根据图中实际形状选择
- 如果无法识别为图表，返回 {"type": "other", "description": "图片内容描述"}
- 只输出 JSON"""


def _encode_image(image_path: str) -> tuple[str, str]:
    """Read and base64-encode an image file, return (base64_data, media_type)."""
    ext = os.path.splitext(image_path)[1].lower()
    media_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    media_type = media_map.get(ext, "image/png")

    with open(image_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return data, media_type


@log_async("tool", "image_to_diagram")
async def run(input_data) -> dict:
    if isinstance(input_data, str):
        image_path = input_data
        extra_prompt = ""
    elif isinstance(input_data, dict):
        image_path = input_data.get("image_path", input_data.get("path", ""))
        extra_prompt = input_data.get("prompt", "")
    else:
        return {"status": "error", "result": "Invalid input: expected str or dict"}

    image_path = os.path.expanduser(image_path)
    if not os.path.exists(image_path):
        return {"status": "error", "result": f"Image file not found: {image_path}"}

    # Try R2 presigned URL first, fall back to base64
    image_url = None
    if r2_storage.is_available():
        try:
            image_url = r2_storage.upload_and_presign(image_path)
        except Exception:
            log.warning("R2 upload failed, falling back to base64", exc_info=True)

    if image_url:
        # Use "media" + "file_uri" so langchain-google-genai sends the URL
        # directly as Gemini fileData instead of downloading → inlineData base64.
        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
        mime_type = mime_map.get(ext, "image/png")
        user_content = [
            {"type": "media", "file_uri": image_url, "mime_type": mime_type},
            {"type": "text", "text": extra_prompt or "请分析这张图片并转换为结构化图表 JSON。"},
        ]
    else:
        try:
            b64_data, media_type = _encode_image(image_path)
        except Exception as e:
            return {"status": "error", "result": f"Failed to read image: {e}"}

        user_content = [
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64_data}"}},
            {"type": "text", "text": extra_prompt or "请分析这张图片并转换为结构化图表 JSON。"},
        ]

    llm = get_llm("doc_analyst")
    messages = [
        SystemMessage(content=DIAGRAM_SYSTEM),
        HumanMessage(content=user_content),
    ]

    try:
        response = await llm.ainvoke(messages)
        return {"status": "ok", "result": response_text(response)}
    except Exception as e:
        return {"status": "error", "result": f"Vision model error: {e}"}
