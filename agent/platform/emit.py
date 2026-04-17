"""统一事件发射器 — 替代业务层散落的 _safe_emit 副本。

用法：
    from agent.platform.emit import safe_emit_progress

    # 高频 transient 进度（不入 DB），支持新旧事件类型名
    safe_emit_progress("progress.step", {"content": "执行中..."})
    safe_emit_progress("step", {"content": "..."})  # 旧名也可以，自动转换

    # 有 session 引用时用 session.emit_event（canonical，入 DB）
    await session.emit_event(task_id, EventType.PROGRESS_STEP, {"content": "..."})

类型名转换：
    业务代码传入旧类型名（如 "step"）时，会通过 OLD_TO_NEW_TYPE_MAP 自动
    转换为新的 category.action 格式（如 "progress.step"），实现平滑过渡。
"""

from __future__ import annotations

from typing import Any


def safe_emit_progress(event_type: str, payload: dict[str, Any]) -> None:
    """Transient progress: 仅写 LangGraph stream，不入 DB/Redis。

    替代各模块的 _safe_emit() 私有副本。
    自动将旧类型名（如 "step"）转换为新格式（如 "progress.step"）。
    """
    try:
        from agent.session.protocol import OLD_TO_NEW_TYPE_MAP
        from langgraph.config import get_stream_writer

        # 旧类型名自动升级到新的 category.action 格式
        normalized_type = OLD_TO_NEW_TYPE_MAP.get(event_type, event_type)
        writer = get_stream_writer()
        writer({"event_type": normalized_type, **payload})
    except Exception:
        pass


def safe_emit_progress_with_content(
    event_type: str, content: str | dict[str, Any]
) -> None:
    """_safe_emit 变体：兼容 content 既可以是 str 也可以是 dict 的模式。"""
    try:
        from agent.session.protocol import OLD_TO_NEW_TYPE_MAP
        from langgraph.config import get_stream_writer

        normalized_type = OLD_TO_NEW_TYPE_MAP.get(event_type, event_type)
        writer = get_stream_writer()
        payload = dict(content) if isinstance(content, dict) else {"content": content}
        payload.setdefault("event_type", normalized_type)
        writer(payload)
    except Exception:
        pass
