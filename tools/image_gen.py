"""
Image Generation Tool — generates images from text prompts via Nano Banana2 API.
Saves generated images to outputs/ directory.
"""
import os
import uuid
import base64
import logging

import httpx

from core.logger import log_async

log = logging.getLogger("chatdada.tools")


# Nano Banana2 API configuration
IMAGE_API_URL = os.getenv("IMAGE_GEN_API_URL", "https://co.yes.vg/v1/chat/completions")
IMAGE_API_KEY = os.getenv("CO_API_KEY", os.getenv("OPENAI_API_KEY", ""))
IMAGE_MODEL = os.getenv("IMAGE_GEN_MODEL", "gemini-3.1-flash-image-landscape")


@log_async("tool", "image_gen")
async def run(input_data) -> dict:
    if isinstance(input_data, str):
        prompts = [input_data]
        size = "1024x1024"
        n = 1
    elif isinstance(input_data, dict):
        raw = input_data.get("prompts", input_data.get("prompt", ""))
        if isinstance(raw, list):
            prompts = raw
        else:
            prompts = [str(raw)]
        size = input_data.get("size", "1024x1024")
        n = input_data.get("n", 1)
    else:
        prompts = [str(input_data)]
        size = "1024x1024"
        n = 1

    prompts = [p for p in prompts if p.strip()]
    if not prompts:
        return {"status": "error", "result": "No prompts provided"}

    if not IMAGE_API_KEY:
        return {
            "status": "error",
            "result": "IMAGE_GEN_API_KEY not configured. "
                      "Set IMAGE_GEN_API_KEY or OPENAI_API_KEY environment variable.",
        }

    os.makedirs("outputs", exist_ok=True)
    generated_files = []
    errors = []

    async with httpx.AsyncClient(timeout=120.0) as client:
        for prompt in prompts:
            try:
                resp = await client.post(
                    IMAGE_API_URL,
                    headers={
                        "Authorization": f"Bearer {IMAGE_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": IMAGE_MODEL,
                        "messages": [
                            {"role": "user", "content": prompt},
                        ],
                    },
                )
                resp.raise_for_status()
                result = resp.json()
                log.info(f"image_gen API response keys: {list(result.keys())}, "
                         f"preview: {str(result)[:500]}")

                # Parse Gemini-style response:
                # candidates[].content.parts[].inlineData.{mimeType, data}
                for candidate in result.get("candidates", []):
                    for part in candidate.get("content", {}).get("parts", []):
                        inline = part.get("inlineData")
                        if inline and inline.get("data"):
                            mime = inline.get("mimeType", "image/png")
                            ext = mime.split("/")[-1] if "/" in mime else "png"
                            file_id = uuid.uuid4().hex[:8]
                            filepath = f"outputs/image_{file_id}.{ext}"
                            with open(filepath, "wb") as f:
                                f.write(base64.b64decode(inline["data"]))
                            generated_files.append(filepath)

            except httpx.HTTPStatusError as e:
                errors.append(f"API error for '{prompt[:50]}': {e.response.status_code} {e.response.text[:200]}")
            except Exception as e:
                errors.append(f"Error for '{prompt[:50]}': {e}")

    if generated_files:
        summary = f"Generated {len(generated_files)} image(s): {', '.join(generated_files)}"
        if errors:
            summary += f"\nWarnings: {'; '.join(errors)}"
        return {"status": "ok", "result": summary, "files": generated_files}

    return {
        "status": "error",
        "result": f"Failed to generate images. Errors: {'; '.join(errors) or 'Unknown error'}",
    }
