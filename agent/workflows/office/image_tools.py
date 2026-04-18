"""Image-related tools for the Office workflow.

提供两类能力：

- ``image_gen``：通过 Nano Banana2 / Gemini Image API 文生图，先把生成的文件落到
  ``outputs/`` 目录，再上传至 Cloudflare R2，返回带预签名 URL 的列表，OfficeCLI 可以
  直接以 ``add ... --type picture --prop src=<url>`` 的形式插图。同时本地路径也一并
  返回，供需要离线/再下载场景使用。

- ``list_user_images``：列出当前会话里用户已经上传到后端的图片素材（即 chat-dada-front
  通过 ``/upload`` 投递、随 task 的 ``file_paths`` / ``reference_files`` 流入 office
  workflow 的图片）。数据由 orchestrator 以 ``configurable.user_images`` 注入到
  langgraph 运行时，本工具通过 ``langgraph.config.get_config()`` 读取——与 task
  生命周期继承一致，天然多任务并发隔离；工具不会扫描后端文件系统。

注意：后端进程跑在服务端，无法直接访问用户本机文件系统。如果产品需要"在用户本机
搜索图片再喂给 office workflow"，必须由 chat-dada-front（运行在用户机上）负责检索
并通过 ``/upload`` 把图片传到后端，前端把返回的 url/path 加入 ``file_paths``，本工具
就能列出。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from agent.tools import image_gen as _image_gen_module
from core import r2_storage

_log = logging.getLogger("chatdada.office.image_tools")

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".svg")
USER_IMAGES_CONFIG_KEY = "user_images"


class ImageGenInput(BaseModel):
    prompt: str = Field(description="图片生成提示词，建议描述画面主体、构图与风格。")
    style: str | None = Field(
        default=None,
        description="可选的整体风格补充，例如 'minimal flat illustration'，会拼接到 prompt 后。",
    )


class ListUserImagesInput(BaseModel):
    pattern: str | None = Field(
        default=None,
        description="可选关键字（不区分大小写）。仅返回 name 包含该关键字的用户上传图片。",
    )


def _is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in _IMAGE_EXTS


def _load_user_images() -> list[dict[str, Any]]:
    """从当前 langgraph runtime 的 ``configurable.user_images`` 读取用户上传图片。

    未运行在 langgraph 上下文时（比如单元测试直接调用工具）返回空列表。
    每个 task 拥有独立的 configurable 上下文，因此天然隔离、并发安全。
    """
    parsed: Any
    try:
        from langgraph.config import get_config

        configurable = get_config().get("configurable", {}) or {}
        parsed = configurable.get(USER_IMAGES_CONFIG_KEY, []) or []
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    images: list[dict[str, Any]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        path = str(entry.get("path") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not name and path:
            name = Path(path).name
        if not name and url:
            name = url.rsplit("/", 1)[-1]
        if not name or not _is_image_name(name):
            continue
        item: dict[str, Any] = {"name": name}
        if path:
            item["path"] = path
        if url:
            item["url"] = url
        images.append(item)
    return images


def _to_picture_src(item: dict[str, Any]) -> str | None:
    """挑出可以直接喂给 OfficeCLI ``--prop src=`` 的引用：优先 url，其次本地 path。"""
    url = str(item.get("url") or "").strip()
    if url:
        return url
    path = str(item.get("path") or "").strip()
    if path and Path(path).exists():
        return path
    return None


@tool("image_gen", args_schema=ImageGenInput)
async def image_gen_tool(prompt: str, style: str | None = None) -> str:
    """生成示意图/插画，落地到 outputs/ 后再上传 R2，返回可直接插图的 URL。

    返回 JSON 字符串，字段：
    - status: "ok" | "error"
    - images: [{name, path, url}]，``url`` 为 R2 预签名地址，可作为
      ``officecli add ... --type picture --prop src=<url>`` 的入参；
    - files: 同 ``images`` 中可用的 src（优先 url，缺省回退本地 path）方便老脚手架使用；
    - message / hint: 说明文字。
    """
    composite = prompt.strip()
    if style:
        composite = f"{composite}\n\nStyle: {style.strip()}"
    if not composite:
        return json.dumps(
            {"status": "error", "images": [], "files": [], "message": "prompt 不能为空"},
            ensure_ascii=False,
        )

    payload = await _image_gen_module.run({"prompt": composite})
    raw_files = list(payload.get("files", []) or [])
    status = str(payload.get("status", "") or "error").lower()
    if status != "ok" or not raw_files:
        return json.dumps(
            {
                "status": "error",
                "images": [],
                "files": [],
                "message": str(payload.get("result", "") or "图片生成失败"),
            },
            ensure_ascii=False,
        )

    r2_ready = r2_storage.is_available()
    images: list[dict[str, Any]] = []
    upload_errors: list[str] = []
    for local_path in raw_files:
        item: dict[str, Any] = {"name": Path(local_path).name, "path": local_path}
        if r2_ready:
            try:
                item["url"] = r2_storage.upload_and_presign(local_path)
            except Exception as exc:  # noqa: BLE001 - log + degrade to local path
                _log.warning("R2 upload failed for %s: %s", local_path, exc)
                upload_errors.append(f"{Path(local_path).name}: {exc}")
        images.append(item)

    src_list = [src for src in (_to_picture_src(item) for item in images) if src]
    response: dict[str, Any] = {
        "status": "ok",
        "images": images,
        "files": src_list,
        "message": str(payload.get("result", "") or ""),
        "hint": (
            "使用 officecli add <file> <parent> --type picture --prop src=<url> 插入图片；"
            "若 R2 不可用则 src 也可以填本地 path。"
        ),
    }
    warnings: list[str] = []
    if upload_errors:
        warnings.extend(upload_errors)
    if not r2_ready:
        warnings.append(
            "R2 storage not configured; only local paths returned. Set R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY to enable presigned URLs.",
        )
    if warnings:
        response["warnings"] = warnings
    return json.dumps(response, ensure_ascii=False)


@tool("list_user_images", args_schema=ListUserImagesInput)
async def list_user_images_tool(pattern: str | None = None) -> str:
    """列出当前会话里用户上传的图片素材，返回 path/url 供 OfficeCLI 插图。

    数据来源：chat-dada-front 通过 ``/upload`` 上传后，url/path 进入 task 的
    ``file_paths``；orchestrated 入口会把其中的图片条目以 ``configurable.user_images``
    注入到 langgraph 运行时，本工具通过 ``get_config()`` 读取，天然隔离多任务。
    """
    images = _load_user_images()
    needle = (pattern or "").strip().lower()
    if needle:
        images = [item for item in images if needle in item["name"].lower()]
    response = {
        "status": "ok",
        "count": len(images),
        "images": images,
        "hint": (
            "把任一条目的 url（优先）或 path 作为 officecli add ... --type picture --prop src=<...> 的入参即可。"
            if images
            else "当前会话没有可用的用户上传图片；可改用 image_gen 生成，或提示用户在前端上传素材。"
        ),
    }
    return json.dumps(response, ensure_ascii=False)


def get_office_image_tools() -> list[Any]:
    """返回 office workflow 可注册的图片工具列表。"""
    return [image_gen_tool, list_user_images_tool]


__all__ = [
    "ImageGenInput",
    "ListUserImagesInput",
    "USER_IMAGES_CONFIG_KEY",
    "get_office_image_tools",
    "image_gen_tool",
    "list_user_images_tool",
]
