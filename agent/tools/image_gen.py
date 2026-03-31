"""
Image Generation Tool — generates images from text prompts via Nano Banana2 API.
Saves generated images to outputs/ directory.
"""
import os
import re
import json
import uuid
import base64
import logging

import httpx

from core.logger import log_async

log = logging.getLogger("chatdada.tools")

_OUTPUTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs")


# Nano Banana2 API configuration
IMAGE_API_URL = os.getenv("IMAGE_GEN_API_URL", "https://co.yes.vg/v1/chat/completions")
IMAGE_API_KEY = os.getenv("CO_API_KEY", os.getenv("OPENAI_API_KEY", ""))
IMAGE_MODEL = os.getenv("IMAGE_GEN_MODEL", "gemini-3.1-flash-image-landscape")


@log_async("tool", "image_gen")
async def run(input_data) -> dict:
    if isinstance(input_data, str):
        prompts = [input_data]
    elif isinstance(input_data, dict):
        raw = input_data.get("prompts", input_data.get("prompt", ""))
        if isinstance(raw, list):
            prompts = raw
        else:
            prompts = [str(raw)]
    else:
        prompts = [str(input_data)]

    prompts = [p for p in prompts if p.strip()]
    if not prompts:
        return {"status": "error", "result": "No prompts provided"}

    if not IMAGE_API_KEY:
        return {
            "status": "error",
            "result": "IMAGE_GEN_API_KEY not configured. "
                      "Set IMAGE_GEN_API_KEY or OPENAI_API_KEY environment variable.",
        }

    os.makedirs(_OUTPUTS_DIR, exist_ok=True)
    generated_files = []
    errors = []

    async with httpx.AsyncClient(timeout=120.0) as client:
        for prompt in prompts:
            try:
                files = await _stream_image(client, prompt)
                generated_files.extend(files)
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
        "result": f"图片生成失败，API 未返回图片数据，请重试。{(' Errors: ' + '; '.join(errors)) if errors else ''}",
    }


async def _stream_image(client: httpx.AsyncClient, prompt: str) -> list[str]:
    """Call the API with stream=true and collect the full response."""
    async with client.stream(
        "POST",
        IMAGE_API_URL,
        headers={
            "Authorization": f"Bearer {IMAGE_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": IMAGE_MODEL,
            "stream": True,
            "messages": [
                {"role": "user", "content": prompt},
            ],
        },
    ) as resp:
        resp.raise_for_status()
        raw_body = await resp.aread()

    # Parse SSE stream: collect all "data: {...}" lines
    text = raw_body.decode("utf-8", errors="replace")
    chunks = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            break
        try:
            chunks.append(json.loads(payload))
        except json.JSONDecodeError:
            continue

    log.info(f"image_gen stream: {len(chunks)} chunks, "
             f"first keys: {list(chunks[0].keys()) if chunks else 'none'}")

    # Extract image data from chunks
    files = []

    # Strategy 1: Gemini-style candidates[].content.parts[].inlineData
    for chunk in chunks:
        for candidate in chunk.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline = part.get("inlineData")
                if inline and inline.get("data"):
                    files.extend(_save_inline(inline))

    # Strategy 2: OpenAI-style delta/message content parts
    if not files:
        b64_parts = []
        for chunk in chunks:
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or choice.get("message") or {}
                content = delta.get("content", "")
                if isinstance(content, str) and content:
                    b64_parts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            inline = part.get("inlineData")
                            if inline and inline.get("data"):
                                files.extend(_save_inline(inline))
        # If all chunks were plain text, check if it contains image URLs
        if not files and b64_parts:
            full_text = "".join(b64_parts)
            files.extend(await _download_image_urls(client, full_text))

    if not files:
        # Collect reasoning_content for diagnostics
        reasoning = []
        for chunk in chunks:
            for choice in chunk.get("choices", []):
                rc = (choice.get("delta") or {}).get("reasoning_content", "")
                if rc:
                    reasoning.append(rc.strip())
        log.warning("image_gen: no images extracted from %d chunks. reasoning=%s",
                    len(chunks), " | ".join(reasoning) if reasoning else "(none)")

    return files


def _save_inline(inline: dict) -> list[str]:
    """Save inlineData to file, return list of paths."""
    mime = inline.get("mimeType", "image/png")
    ext = mime.split("/")[-1] if "/" in mime else "png"
    file_id = uuid.uuid4().hex[:8]
    filepath = os.path.join(_OUTPUTS_DIR, f"image_{file_id}.{ext}")
    with open(filepath, "wb") as f:
        f.write(base64.b64decode(inline["data"]))
    return [filepath]


# Matches markdown image links and bare https URLs ending with image-like paths
_IMAGE_URL_RE = re.compile(
    r'!\[[^\]]*\]\((https?://[^\s\)]+)\)'  # ![alt](url)
    r'|'
    r'(https?://storage\.googleapis\.com/[^\s\)\"\']+)'  # bare GCS URL
)


async def _download_image_urls(client: httpx.AsyncClient, text: str) -> list[str]:
    """Extract image URLs from text and download them."""
    urls = [m.group(1) or m.group(2) for m in _IMAGE_URL_RE.finditer(text)]
    if not urls:
        log.info(f"image_gen: no image URLs found in text: {text[:200]}")
        return []

    files = []
    for url in urls:
        try:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "image/png")
            ext = "png"
            if "jpeg" in ct or "jpg" in ct:
                ext = "jpg"
            elif "webp" in ct:
                ext = "webp"
            file_id = uuid.uuid4().hex[:8]
            filepath = os.path.join(_OUTPUTS_DIR, f"image_{file_id}.{ext}")
            with open(filepath, "wb") as f:
                f.write(resp.content)
            files.append(filepath)
            log.info(f"image_gen: downloaded {url[:80]}... -> {filepath}")
        except Exception as e:
            log.warning(f"image_gen: failed to download {url[:80]}...: {e}")
    return files
