from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable as AbcCallable
from datetime import UTC
from typing import Any, Awaitable

import redis.asyncio as aioredis
from langgraph.types import Command

from agent.session.protocol import EventType
from agent.session.runtime import SessionRuntime, is_transient_progress_type
from agent.platform.state import RouteDecisionPayload
from agent.hands import DesktopHandsManager, DesktopToolExecutor, LocalToolExecutor, ToolGateway
from agent.runtime.interaction import (
    reset_preloaded_user_replies,
    reset_task_interaction_handler,
    set_preloaded_user_replies,
    set_task_interaction_handler,
)
from agent.runtime.root_graph import build_root_graph
from agent.runtime.cost_logging import (
    attach_partial_progress,
    attach_quality_summary,
    build_failure_diagnostics,
    init_cost_ledger,
    merge_llm_usage_into_ledger,
    merge_tool_events_into_ledger,
    summarize_cost_ledger,
)
from agent.domains.office.core.quality_report import quality_report_summary_lines, summarize_quality_report
from core.langsmith_config import build_langsmith_run_config
from core.logger import monitor, new_trace_id
from core.models import set_thinking_level
from domain.billing.services import QuotaExceededError, QuotaService
from infra.db.repositories.quota_repo import UsageEventRepository, UserQuotaRepository
from infra.db.session import SessionFactory
from agent.platform.streaming import extract_checkpoint_id, translate_stream_part

log = logging.getLogger("chatdada.tasks")

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
HEARTBEAT_INTERVAL_SECONDS = 10


def compose_task_text(task: str, file_paths: list[str]) -> str:
    if not file_paths:
        return task
    file_list = "\n".join(f"  - {path}" for path in file_paths)
    return f"{task}\n\n[用户上传了以下文件，请在任务中使用这些文件]:\n{file_list}"


def task_is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def parse_step_payload(step_info: str) -> tuple[str, dict[str, Any]]:
    """解析 step 回调 payload，返回 (event_type, payload) 元组。

    event_type 统一使用新的 category.action 格式（通过 OLD_TO_NEW_TYPE_MAP 自动转换）。
    """
    from agent.session.protocol import OLD_TO_NEW_TYPE_MAP

    try:
        parsed = json.loads(step_info)
    except (json.JSONDecodeError, TypeError):
        return "progress.step", {"content": str(step_info)}

    if isinstance(parsed, dict) and isinstance(parsed.get("type"), str):
        payload = dict(parsed)
        raw_type = str(payload.pop("type"))
        # 旧类型名（如 "file"、"step"）自动映射到新格式
        event_type = OLD_TO_NEW_TYPE_MAP.get(raw_type, raw_type)
        if event_type == "artifact.created":
            payload.setdefault("content", payload.get("name") or payload.get("url") or "")
        else:
            payload.setdefault("content", str(payload.get("content", "")))
        return event_type, payload

    return "progress.step", {"content": str(step_info)}


def _merge_nested_interrupt_pending(current_pending: bool, payload: dict[str, Any]) -> bool:
    return current_pending or bool(payload.get("nested_graph"))


def _merge_quality_report_summary(
    quality_report: dict[str, Any] | None,
    *summary_sources: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = summarize_quality_report(quality_report)
    for source in summary_sources:
        if isinstance(source, dict):
            merged.update({key: value for key, value in source.items() if value is not None})
    return merged


class TaskService:
    """Harness Runtime — 无状态编排层。

    只依赖 Session 和 Hands 的稳定接口：
    - SessionRuntime: emit_event, get_events, get_projection, wake, record_transition
    - ToolGateway: execute (Phase 3 逐步接入)

    本地保留 _runner_tasks 作为 process-local cancel handle。
    """

    def __init__(
        self,
        *,
        session: SessionRuntime,
        redis: aioredis.Redis,
        checkpointer_factory: AbcCallable[[], Any],
        conversation_context_builder_factory: AbcCallable[[], Any],
        embedding_service: Any | None = None,
        conversation_service: Any | None = None,
    ) -> None:
        self._session = session
        self._redis = redis
        self._checkpointer_factory = checkpointer_factory
        self._conversation_context_builder_factory = conversation_context_builder_factory
        self._embedding_service = embedding_service
        self._conversation_service = conversation_service
        self._desktop_manager: DesktopHandsManager | None = None
        self._desktop_executor: DesktopToolExecutor | None = None
        self._tool_gateway: ToolGateway | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._runner_tasks: dict[str, asyncio.Task[Any]] = {}
        self._checkpointer_cm: Any | None = None
        self._checkpointer: Any | None = None
        self._root_graph: Any | None = None

    def configure_desktop(
        self,
        *,
        manager: DesktopHandsManager,
        executor: DesktopToolExecutor,
    ) -> None:
        self._desktop_manager = manager
        self._desktop_executor = executor
        self._tool_gateway = ToolGateway(
            local=LocalToolExecutor(),
            session=self._session,
            desktop_manager=manager,
            desktop_executor=executor,
        )

    async def _open_checkpointer(self) -> Any:
        candidate = self._checkpointer_factory()
        if hasattr(candidate, "__aenter__") and hasattr(candidate, "__aexit__"):
            self._checkpointer_cm = candidate
            checkpointer = await candidate.__aenter__()
        else:
            checkpointer = await candidate if asyncio.iscoroutine(candidate) else candidate
        if hasattr(checkpointer, "setup"):
            await checkpointer.setup()
        return checkpointer

    async def connect(self) -> None:
        await self._session.setup()
        self._checkpointer = await self._open_checkpointer()
        self._root_graph = build_root_graph(checkpointer=self._checkpointer)
        await self._recover_interrupted_tasks()

    async def close(self) -> None:
        for task in list(self._runner_tasks.values()):
            task.cancel()
        self._runner_tasks.clear()
        if self._redis:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None
        if self._checkpointer_cm is not None:
            await self._checkpointer_cm.__aexit__(None, None, None)
            self._checkpointer_cm = None
            self._checkpointer = None
            self._root_graph = None

    @property
    def session(self) -> SessionRuntime:
        """SessionRuntime instance — the durable state boundary."""
        return self._session

    @property
    def redis(self) -> aioredis.Redis:
        return self._redis

    def list_runtime_desktop_tools(self, user_id: str) -> list[dict[str, Any]]:
        if self._desktop_manager is None:
            return []
        return self._desktop_manager.list_tool_descriptors(user_id)

    def _track_runner(self, task_id: str, task: asyncio.Task[Any]) -> None:
        self._runner_tasks[task_id] = task
        self._background_tasks.add(task)

        def _cleanup(_task: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(_task)
            if self._runner_tasks.get(task_id) is _task:
                self._runner_tasks.pop(task_id, None)

        task.add_done_callback(_cleanup)

    async def _recover_interrupted_tasks(self) -> None:
        if self._session is None:
            return

        task_ids = await self._session.recover_interrupted_tasks()
        if not task_ids:
            return

        log.warning(
            "Recovering %s interrupted task(s) after process restart via Session.wake",
            len(task_ids),
        )
        for task_id in task_ids:
            snapshot = await self._session.get_task(task_id) or {}
            if snapshot.get("status") == "queued":
                background = asyncio.create_task(
                    self._execute_task(task_id),
                    name=f"task-requeue-{task_id}",
                )
                self._track_runner(task_id, background)
                continue
            if snapshot.get("status") == "waiting_for_user":
                log.info("Preserving waiting_for_user task without auto-restart: %s", task_id)
                continue

            try:
                resume_handle = await self._session.wake(task_id)
            except Exception:
                log.exception("Failed to build resume handle for interrupted task %s", task_id)
                continue

            background = asyncio.create_task(
                self._execute_task(task_id, resume_handle=resume_handle),
                name=f"task-recover-{task_id}",
            )
            self._track_runner(task_id, background)

    async def _finalize_cancelled(self, task_id: str, message: str = "任务已取消") -> dict[str, Any] | None:
        snapshot = await self._session.get_task(task_id)
        if snapshot is None:
            return None
        if snapshot.get("status") == "cancelled":
            return snapshot

        await self._session.update_projection(
            task_id,
            projection_patch={"pending_question": None, "cancel_state": None},
        )
        await self.record_event(
            task_id,
            EventType.LIFECYCLE_CANCELLED.value,
            {"message": message},
        )
        await self._session.cancel_task(task_id, error_text=message)
        return await self._session.get_task(task_id)

    async def submit_task(
        self,
        *,
        task_text: str,
        user_id: str,
        mode: str,
        thinking_level: str,
        file_paths: list[str],
        conversation_id: str = "",
    ) -> dict[str, Any]:
        async with SessionFactory() as session:
            quota_service = QuotaService(UserQuotaRepository(session), UsageEventRepository(session))
            try:
                quota_snapshots = await quota_service.assess_before_task(user_id=user_id)
            except QuotaExceededError as exc:
                raise RuntimeError(exc.user_message) from exc

        request_payload = {
            "task": task_text,
            "user_id": user_id,
            "mode": mode,
            "thinking_level": thinking_level,
            "file_paths": file_paths,
            "quota": [
                {
                    "period": item.period,
                    "tasks_used": item.tasks_used,
                    "tasks_limit": item.tasks_limit,
                    "tokens_used": item.tokens_used,
                    "tokens_limit": item.tokens_limit,
                    "cost_used_usd": item.cost_used_usd,
                    "cost_limit_usd": item.cost_limit_usd,
                }
                for item in quota_snapshots
            ],
        }
        snapshot = await self._session.create_task(
            user_id=user_id,
            task_text=task_text,
            mode=mode,
            thinking_level=thinking_level,
            request_payload=request_payload,
            conversation_id=conversation_id,
        )
        background = asyncio.create_task(
            self._execute_task(snapshot["task_id"]),
            name=f"task-runner-{snapshot['task_id']}",
        )
        self._track_runner(snapshot["task_id"], background)
        return snapshot

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await self._session.get_task(task_id)

    async def get_events_after(self, task_id: str, after_seq: int) -> list[dict[str, Any]]:
        return await self._session.get_events_after(task_id, after_seq)

    async def subscribe(self, task_id: str) -> aioredis.client.PubSub:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(f"task:{task_id}:events")
        return pubsub

    async def unsubscribe(self, task_id: str, pubsub: aioredis.client.PubSub) -> None:
        await pubsub.unsubscribe(f"task:{task_id}:events")
        await pubsub.aclose()

    async def record_event(
        self, task_id: str, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Route canonical events to history and progress events to stream only."""
        if is_transient_progress_type(event_type):
            await self._session.emit_progress(task_id, event_type, payload)
            return None
        return await self._session.emit_event(task_id, event_type, payload)

    async def request_user_input(
        self, task_id: str, question_payload: dict[str, Any]
    ) -> str:
        snapshot = await self._session.get_task(task_id)
        if snapshot is None:
            raise RuntimeError("任务不存在，无法请求用户补充。")

        content = str(question_payload.get("content", "") or "").strip()
        if not content:
            raise ValueError("追问内容不能为空。")

        payload = {
            "content": content,
            "context": str(question_payload.get("context", "") or "").strip(),
            "placeholder": str(question_payload.get("placeholder", "") or "").strip(),
        }

        await self._session.set_waiting_for_user(task_id, payload)
        await self.record_event(task_id, EventType.INTERACTION_QUESTION.value, payload)
        raise RuntimeError("request_user_input is now graph-interrupt driven and should not be awaited directly")

    async def reply_to_task(self, task_id: str, answer: str) -> dict[str, Any]:
        snapshot = await self._session.get_task(task_id)
        if snapshot is None:
            raise KeyError(task_id)

        answer_text = str(answer or "").strip()
        if not answer_text:
            raise ValueError("回复内容不能为空。")

        if snapshot["status"] != "waiting_for_user":
            raise RuntimeError("任务当前不在等待用户回复。")

        await self._session.resume_task(task_id)
        await self.record_event(task_id, EventType.INTERACTION_ANSWER.value, {"content": answer_text})
        background = asyncio.create_task(
            self._execute_task(task_id, resume_value=answer_text),
            name=f"task-resume-{task_id}",
        )
        self._track_runner(task_id, background)
        return await self._session.get_task(task_id) or snapshot

    async def cancel_running_task(self, task_id: str) -> dict[str, Any]:
        snapshot = await self._session.get_task(task_id)
        if snapshot is None:
            raise KeyError(task_id)
        if task_is_terminal(snapshot["status"]):
            raise RuntimeError("任务已经结束，无法取消。")

        await self.record_event(
            task_id,
            EventType.LIFECYCLE_CANCELLED.value,
            {"content": "已请求取消任务"},
        )
        await self._session.update_projection(
            task_id,
            projection_patch={"cancel_state": "requested"},
        )
        await self._session.request_cancel(task_id)

        runner = self._runner_tasks.get(task_id)
        if runner is None:
            updated = await self._session.update_projection(
                task_id,
                projection_patch={"cancel_state": "cancelling"},
            )
            return updated or snapshot

        await self._session.update_projection(
            task_id,
            projection_patch={"cancel_state": "cancelling"},
        )
        if runner is not None:
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
            except Exception:
                log.warning("Runner raised while cancelling task %s", task_id, exc_info=True)

        latest = await self._session.get_task(task_id)
        if latest and latest.get("status") == "cancelled":
            return latest
        cancelled = await self._finalize_cancelled(task_id)
        return cancelled or snapshot

    async def _execute_task(
        self,
        task_id: str,
        *,
        resume_value: str | None = None,
        resume_handle: Any | None = None,
    ) -> None:
        snapshot = await self._session.get_task(task_id)
        if snapshot is None:
            return
        if resume_value is not None and resume_handle is not None:
            raise ValueError("resume_value and resume_handle are mutually exclusive")

        task_text = snapshot["task"]
        user_id = snapshot["user_id"]
        mode = snapshot["mode"]
        thinking_level = snapshot["thinking_level"]
        file_paths = snapshot.get("file_paths", [])
        conversation_id = snapshot.get("conversation_id", "")
        execution_task = compose_task_text(task_text, file_paths)
        trace_id = new_trace_id()
        interrupted = False
        is_human_resume = resume_value is not None
        is_recovery_resume = resume_handle is not None
        latest_checkpoint_id = str(
            (getattr(resume_handle, "checkpoint_id", None) if resume_handle is not None else None)
            or snapshot.get("latest_checkpoint_id", "")
            or ""
        )
        resume_last_step_content = ""
        skipped_resume_replay_step = False

        decision = None
        route_payload = snapshot.get("initial_route_payload")
        if not is_human_resume and not is_recovery_resume:
            # C7: Dispatcher routing deleted — Coordinator's understand_goal does routing internally.
            # Use minimal placeholder route_payload; actual routing happens in run_coordinator.
            route_payload: RouteDecisionPayload = {
                "route_name": "general_chat",
                "reason": "placeholder for coordinator routing",
                "confidence": 0.0,
                "execution_path": "general_chat",
            }
            public_route_name = route_payload["route_name"]
            await self._session.set_route_info(
                task_id,
                route_name=public_route_name,
                route_reason=route_payload["reason"],
                route_confidence=route_payload["confidence"],
            )
            await self._session.mark_started(task_id)
            await self._session.update_projection(
                task_id,
                projection_patch={
                    "latest_checkpoint_id": "",
                    "artifact_refs": [],
                    "nested_interrupt_pending": False,
                    "pending_question": None,
                    "review": None,
                    "budget": None,
                    "cancel_state": None,
                },
                request_payload_patch={
                    "domain": route_payload["execution_path"],
                    "execution_path": route_payload["execution_path"],
                },
            )
            await self.record_event(task_id, EventType.LIFECYCLE_STARTED.value, {"content": f"开始执行: {execution_task}"})
            log.info("Task received user=%s task=%s", user_id, task_text[:80])
        else:
            stored_request = snapshot.get("request_payload", {})
            if not isinstance(stored_request, dict):
                stored_request = {}
            route_payload = {
                "route_name": snapshot.get("route_name", ""),
                "reason": snapshot.get("route_reason", ""),
                "confidence": snapshot.get("route_confidence", 0.0),
                "execution_path": stored_request.get(
                    "execution_path",
                    snapshot.get("route_name", ""),
                ),
            }
            existing_events = await self._session.get_events_after(task_id, 0)
            for event in reversed(existing_events):
                if event["type"] == "progress.step":
                    # 新 envelope 格式：内容在 payload 中
                    payload = event.get("payload") or {}
                    resume_last_step_content = str(payload.get("content", "") or "")
                    break

        conversation_context = ""
        if conversation_id and not is_human_resume and not is_recovery_resume:
            try:
                ctx = await self._conversation_context_builder_factory().build(
                    conversation_id, task_text
                )
                conversation_context = ctx.text
                if conversation_context:
                    log.info(
                        "Conversation context built: strategy=%s rounds=%d len=%d",
                        ctx.strategy, ctx.round_count, len(conversation_context),
                    )
            except Exception as exc:
                log.warning("Failed to build conversation context: %s", exc)

        async def on_step(step_info: str) -> None:
            event_type, payload = parse_step_payload(step_info)
            await self.record_event(task_id, event_type, payload)

        interaction_token = set_task_interaction_handler(None)
        preloaded_replies_token = set_preloaded_user_replies(None)
        try:
            set_thinking_level(thinking_level)
            request_payload = snapshot.get("request_payload", {})
            if not isinstance(request_payload, dict):
                request_payload = {}
            clarification_history = await self._session.get_clarification_history(task_id)
            if clarification_history:
                request_payload = {
                    **request_payload,
                    "clarification_history": clarification_history,
                }
            if resume_handle is not None:
                request_payload = {
                    **request_payload,
                    **{
                        key: value
                        for key, value in (resume_handle.resume_context or {}).items()
                        if value is not None
                    },
                }
            nested_interrupt_pending = bool(
                request_payload.get(
                    "nested_interrupt_pending",
                    snapshot.get("nested_interrupt_pending", False),
                )
            )
            request_payload["nested_interrupt_pending"] = nested_interrupt_pending
            if resume_value is not None and nested_interrupt_pending:
                replay_replies = [
                    str(item.get("answer", "") or "")
                    for item in clarification_history
                    if isinstance(item, dict)
                    and str(item.get("nested_graph", "") or "").strip()
                    and str(item.get("answer", "") or "").strip()
                ]
                reset_preloaded_user_replies(preloaded_replies_token)
                preloaded_replies_token = set_preloaded_user_replies(replay_replies)
            initial_state = {
                "task_id": task_id,
                "thread_id": task_id,
                "user_id": user_id,
                "mode": mode,
                "thinking_level": thinking_level,
                "task_text": task_text,
                "execution_task": execution_task,
                "file_paths": file_paths,
                "conversation_id": conversation_id,
                "conversation_context": conversation_context,
                "request_payload": dict(request_payload),
                "initial_route_payload": route_payload,
            }
            config = {
                "configurable": {
                    "thread_id": task_id,
                    "nested_interrupt_count": 0,
                    "nested_resume_value": None,
                    "session": self._session,
                    "conversation_service": self._conversation_service,
                    "desktop_manager": self._desktop_manager,
                    "tool_gateway": self._tool_gateway,
                    "request_user_id": user_id,
                }
            }
            if latest_checkpoint_id:
                config["configurable"]["checkpoint_id"] = latest_checkpoint_id
            checkpoint_ns = str(
                getattr(resume_handle, "checkpoint_ns", "") if resume_handle is not None else ""
            ).strip()
            if checkpoint_ns:
                config["configurable"]["checkpoint_ns"] = checkpoint_ns
            ls_config = build_langsmith_run_config(
                task_id=task_id,
                user_id=user_id,
                domain=route_payload.get("route_name", ""),
                mode=mode,
            )
            if ls_config:
                config.update(ls_config)
            stream_input: Any
            if resume_handle is not None:
                if resume_handle.stream_input is not None:
                    stream_input = resume_handle.stream_input
                else:
                    stream_input = None if latest_checkpoint_id else initial_state
            elif resume_value is not None and nested_interrupt_pending:
                stream_input = initial_state
            else:
                stream_input = initial_state if resume_value is None else Command(resume=resume_value)
            current_pending_question: dict[str, Any] | None = None

            async for part in self._root_graph.astream(
                stream_input,
                config=config,
                version="v2",
                stream_mode=["updates", "messages", "custom", "tasks", "checkpoints"],
                subgraphs=True,
            ):
                checkpoint_id = extract_checkpoint_id(part)
                if checkpoint_id:
                    latest_checkpoint_id = checkpoint_id
                    await self._session.update_projection(
                        task_id,
                        projection_patch={"latest_checkpoint_id": checkpoint_id},
                    )
                for event_type, payload in translate_stream_part(
                    part,
                    thread_id=task_id,
                    domain=route_payload["route_name"],
                    checkpoint_id=latest_checkpoint_id,
                    trace_metadata={
                        "trace_id": trace_id,
                        "task_id": task_id,
                        "domain": route_payload["route_name"],
                        "mode": mode,
                    },
                ):
                    if event_type == "interaction.question":
                        if (
                            interrupted
                            and not payload.get("nested_graph")
                            and str(payload.get("content", "") or "") == str((current_pending_question or {}).get("content", "") or "")
                        ):
                            continue
                        interrupted = True
                        nested_interrupt_pending = _merge_nested_interrupt_pending(
                            nested_interrupt_pending,
                            payload,
                        )
                        await self._session.set_waiting_for_user(
                            task_id,
                            payload,
                            latest_checkpoint_id=latest_checkpoint_id,
                            nested_interrupt_pending=nested_interrupt_pending,
                        )
                        current_pending_question = payload
                    if (
                        resume_value is not None
                        and event_type == "progress.step"
                        and not skipped_resume_replay_step
                        and str(payload.get("content", "") or "") == resume_last_step_content
                    ):
                        skipped_resume_replay_step = True
                        continue
                    if (
                        resume_value is not None
                        and event_type == "progress.node"
                        and str(payload.get("node_name", "") or "") in {"run_research", "run_patent", "run_zero_report", "run_ppt"}
                    ):
                        update = payload.get("update") if isinstance(payload.get("update"), dict) else {}
                        update_metadata = payload.get("update_metadata") if isinstance(payload.get("update_metadata"), dict) else {}
                        final_result = str(update.get("final_result", "") or "")
                        if update_metadata.get("cached") and final_result.endswith("未生成最终结果。"):
                            log.warning(
                                "Resume reused cached fallback domain result: task_id=%s node=%s final_result=%s",
                                task_id,
                                payload.get("node_name", ""),
                                final_result,
                            )
                    await self.record_event(task_id, event_type, payload)

            if interrupted:
                summary = monitor.get_summary(trace_id)
                summary.update({"interrupted": True, "waiting_for_user": True})
                await self.record_event(task_id, EventType.SYSTEM_MONITORING.value, {"content": summary})
                monitor.finalize(trace_id)
                return

            state_snapshot = await self._root_graph.aget_state(config)
            final_values = getattr(state_snapshot, "values", {}) or {}
            result = str(final_values.get("final_result", "") or "")
            artifact_refs = final_values.get("artifact_refs", []) or []
            review = final_values.get("review") or {}
            budget = final_values.get("budget") or {}
            research_strategy = str(final_values.get("research_strategy", "") or "")
            await self._session.set_result_text(task_id, result)
            await self._session.update_projection(
                task_id,
                projection_patch={
                    "artifact_refs": artifact_refs,
                    "pending_question": None,
                    "latest_checkpoint_id": latest_checkpoint_id,
                    "nested_interrupt_pending": False,
                    "review": review or None,
                    "budget": budget or None,
                    "cancel_state": None,
                },
                request_payload_patch={
                    "research_strategy": research_strategy,
                }
                if research_strategy
                else None,
            )
            event_payload: dict[str, Any] = {
                "content": result,
                "artifact_refs": artifact_refs,
                "thread_id": task_id,
            }
            if research_strategy:
                event_payload["research_strategy"] = research_strategy
            await self.record_event(task_id, EventType.LIFECYCLE_COMPLETED.value, event_payload)
        except asyncio.CancelledError:
            log.info("Task cancelled: %s", task_id)
            await self._finalize_cancelled(task_id)
            monitor.finalize(trace_id)
            return
        except Exception as exc:
            error_text = str(exc)
            error_code = "task_execution_error"
            user_message = error_text
            if "weekly_limit_exceeded" in error_text:
                error_code = "provider_weekly_limit_exceeded"
                user_message = "服务端上游模型本周额度已用完，请稍后再试。"
            elif "daily_limit_exceeded" in error_text:
                error_code = "provider_daily_limit_exceeded"
                user_message = "服务端上游模型当日额度已用完，请稍后再试。"
            elif "monthly_limit_exceeded" in error_text:
                error_code = "provider_monthly_limit_exceeded"
                user_message = "服务端上游模型本月额度已用完，请稍后再试。"
            log.error("Task failed: %s", exc)
            await self._session.set_error_text(task_id, error_text)
            await self.record_event(
                task_id,
                EventType.LIFECYCLE_FAILED.value,
                {
                    "content": user_message,
                    "error_code": error_code,
                    "raw_error": error_text,
                },
            )
            summary = monitor.get_summary(trace_id)
            await self.record_event(task_id, EventType.SYSTEM_MONITORING.value, {"content": summary})
            await self._session.finish_task(task_id, "failed")
            monitor.finalize(trace_id)
            return
        finally:
            reset_preloaded_user_replies(preloaded_replies_token)
            reset_task_interaction_handler(interaction_token)

        summary = monitor.get_summary(trace_id)
        events = await self._session.get_events(task_id)
        async with SessionFactory() as session:
            usage_service = QuotaService(UserQuotaRepository(session), UsageEventRepository(session))
            llm_usage = list(summary.get("llm_usage", []) or [])
            total_input_tokens = int(sum(int(item.get("input_tokens", 0) or 0) for item in llm_usage))
            total_output_tokens = int(sum(int(item.get("output_tokens", 0) or 0) for item in llm_usage))
            primary_model = str(llm_usage[0].get("model", "") or "") if llm_usage else ""
            estimated_cost_usd = usage_service.estimate_cost_from_usage(llm_usage)

            latest_snapshot = await self._session.get_task(task_id) or {}
            budget = dict(latest_snapshot.get("budget") or {})
            review = dict(latest_snapshot.get("review") or {})
            artifact_refs = list(latest_snapshot.get("artifact_refs") or [])
            quality_report = dict(review.get("quality_report") or {})
            cost_ledger = dict(budget.get("cost_ledger") or {})
            quality_report_summary = _merge_quality_report_summary(
                quality_report,
                review.get("quality_report_summary") if isinstance(review.get("quality_report_summary"), dict) else None,
                budget.get("quality_report_summary") if isinstance(budget.get("quality_report_summary"), dict) else None,
                cost_ledger.get("quality_report_summary") if isinstance(cost_ledger.get("quality_report_summary"), dict) else None,
            )
            partial_progress = dict(review.get("partial_progress") or cost_ledger.get("partial_progress") or {})
            if not cost_ledger:
                cost_ledger = init_cost_ledger(task_id=task_id, domain="office")
            cost_ledger = merge_tool_events_into_ledger(cost_ledger, events=events)
            cost_ledger = merge_llm_usage_into_ledger(
                cost_ledger,
                llm_usage=llm_usage,
                estimate_cost=usage_service.estimate_cost_usd,
            )
            diagnostics = build_failure_diagnostics(events)
            if diagnostics.get("completed_pages"):
                cost_ledger["completed_pages"] = int(diagnostics["completed_pages"])
            elif partial_progress.get("completed_pages"):
                cost_ledger["completed_pages"] = int(partial_progress["completed_pages"])
            cost_ledger = attach_partial_progress(cost_ledger, partial_progress=partial_progress)
            cost_ledger = attach_quality_summary(
                cost_ledger,
                quality_report_summary=quality_report_summary,
            )
            budget["cost_ledger"] = summarize_cost_ledger(cost_ledger)
            budget["monitoring_summary"] = summary
            if quality_report:
                budget["quality_report_summary"] = quality_report_summary
                review["quality_report_summary"] = quality_report_summary

            result_text = str(latest_snapshot.get("result_text", "") or "")
            if (
                "inner_recursion_limit" in str(review.get("reason", "") or "")
                or "内层 agent 超过" in result_text
                or not bool(review.get("passed", True))
            ):
                detail_lines = []
                if diagnostics.get("current_stage"):
                    detail_lines.append(f"当前阶段: {diagnostics['current_stage']}")
                if diagnostics.get("completed_pages"):
                    detail_lines.append(f"已推断完成页数: {diagnostics['completed_pages']}")
                elif partial_progress.get("completed_pages"):
                    detail_lines.append(f"已完成页数: {partial_progress['completed_pages']}")
                last_success = diagnostics.get("last_successful_tool") or {}
                if last_success.get("command"):
                    detail_lines.append(f"最后一次成功工具调用: {last_success['command']}")
                detail_lines.extend(quality_report_summary_lines(quality_report))
                if detail_lines:
                    result_text = f"{result_text}\n" + "\n".join(detail_lines)
                    await self._session.set_result_text(task_id, result_text)

            await self._session.update_projection(
                task_id,
                projection_patch={
                    "budget": budget or None,
                    "review": review or None,
                    "artifact_refs": artifact_refs,
                },
            )
            await self.record_event(
                task_id,
                EventType.SYSTEM_MONITORING.value,
                {
                    "content": summary,
                    "cost_ledger": budget["cost_ledger"],
                    "diagnostics": diagnostics,
                    "quality_report_summary": quality_report_summary,
                },
            )
            await usage_service.record_task_usage(
                user_id=user_id,
                task_id=task_id,
                model=primary_model,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                total_tokens=int(summary.get("total_tokens", 0) or 0),
                cost_usd=estimated_cost_usd,
            )
            await session.commit()
        await self._session.finish_task(task_id, "succeeded")
        monitor.finalize(trace_id)

        if conversation_id and self._embedding_service is not None:
            try:
                bg = asyncio.create_task(
                    self._embedding_service.generate_embeddings(task_id),
                    name=f"embed-{task_id}",
                )
                self._background_tasks.add(bg)
                bg.add_done_callback(self._background_tasks.discard)
            except Exception as exc:
                log.warning("Failed to schedule embedding generation: %s", exc)


def format_sse(event: dict[str, Any]) -> str:
    """将事件格式化为 SSE 帧。

    SSE 帧格式（符合 Layer 0 spec）：
        id: {seq}          ← 仅 canonical 事件（有 seq 字段）
        event: {type}      ← 事件类型（category.action 格式）
        data: {json}       ← 完整 JSON envelope（type+taskId+timestamp+seq?+payload）
    """
    lines = []
    # canonical 事件带 seq，transient 事件无 seq — 通过 seq 是否存在来判断
    if event.get("seq") is not None:
        lines.append(f"id: {event['seq']}")
    lines.append(f"event: {event['type']}")
    lines.append(f"data: {json.dumps(event, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "TaskService",
    "compose_task_text",
    "format_sse",
    "parse_step_payload",
    "task_is_terminal",
]
