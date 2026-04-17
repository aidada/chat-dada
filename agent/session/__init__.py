"""Session 层 — 系统唯一的 durable state boundary。

职责：
- 追加 canonical 事件 (emit_event)
- 发送 transient 进度 (emit_progress)
- 暴露历史读取接口 (get_events)
- 提供 wake/recovery 能力
- 维护 projection (task_runs)
"""

from agent.session.runtime import EventType, ResumeHandle, SessionRuntime

__all__ = ["EventType", "ResumeHandle", "SessionRuntime"]
