from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import uuid
from collections import defaultdict
from collections.abc import Callable as AbcCallable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable

from logger import monitor, new_trace_id
from models import set_thinking_level
from task_dispatcher import RouteDecision, dispatch_task
from task_interaction import reset_task_interaction_handler, set_task_interaction_handler

log = logging.getLogger("chatdada.tasks")

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
HEARTBEAT_INTERVAL_SECONDS = 10

TaskDispatcher = AbcCallable[[str, list[str], str, str], Awaitable[RouteDecision]]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def compose_task_text(task: str, file_paths: list[str]) -> str:
    if not file_paths:
        return task
    file_list = "\n".join(f"  - {path}" for path in file_paths)
    return f"{task}\n\n[用户上传了以下文件，请在任务中使用这些文件]:\n{file_list}"


def task_is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def parse_step_payload(step_info: str) -> tuple[str, dict[str, Any]]:
    try:
        parsed = json.loads(step_info)
    except (json.JSONDecodeError, TypeError):
        return "step", {"content": str(step_info)}

    if isinstance(parsed, dict) and isinstance(parsed.get("type"), str):
        payload = dict(parsed)
        event_type = str(payload.pop("type"))
        if event_type == "file":
            payload.setdefault("content", payload.get("name") or payload.get("url") or "")
        else:
            payload.setdefault("content", str(payload.get("content", "")))
        return event_type, payload

    return "step", {"content": str(step_info)}


class TaskRunStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        self._recover_interrupted_tasks()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_runs (
                    task_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_text TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'auto',
                    thinking_level TEXT NOT NULL,
                    route_name TEXT,
                    route_reason TEXT,
                    route_confidence REAL,
                    request_payload_json TEXT NOT NULL,
                    result_text TEXT,
                    error_text TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    task_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, seq)
                );

                CREATE INDEX IF NOT EXISTS idx_task_events_task_seq
                ON task_events (task_id, seq);
                """
            )
            self._ensure_task_run_columns()
            self._conn.commit()

    def _ensure_task_run_columns(self) -> None:
        existing_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(task_runs)")
        }
        required_columns = {
            "mode": "TEXT NOT NULL DEFAULT 'auto'",
            "route_name": "TEXT",
            "route_reason": "TEXT",
            "route_confidence": "REAL",
            "pending_question_json": "TEXT",
        }

        for column_name, column_sql in required_columns.items():
            if column_name in existing_columns:
                continue
            self._conn.execute(
                f"ALTER TABLE task_runs ADD COLUMN {column_name} {column_sql}"
            )

    def _recover_interrupted_tasks(self) -> None:
        interrupted = self._fetchall(
            """
            SELECT task_id
            FROM task_runs
            WHERE status IN ('queued', 'running', 'waiting_for_user')
            ORDER BY created_at ASC
            """
        )
        if not interrupted:
            return

        log.warning("Recovering %s interrupted task(s) after process restart", len(interrupted))
        for row in interrupted:
            task_id = row["task_id"]
            snapshot = self.get_task(task_id) or {}
            if snapshot.get("status") == "waiting_for_user":
                message = "任务在等待用户补充时因服务重启而中断，请重新提交。"
            else:
                message = "任务因服务重启而中断，请重新提交。"
            self.set_error_text(task_id, message)
            self.append_event(
                task_id,
                "error",
                {"content": message, "recovered": True},
            )
            self.append_event(
                task_id,
                "monitoring",
                {
                    "content": {
                        "trace_id": None,
                        "total_duration_ms": 0,
                        "llm_call_count": 0,
                        "total_tokens": 0,
                        "error_count": 1,
                        "events": [],
                        "interrupted": True,
                    }
                },
            )
            self.finish_task(task_id, "failed")

    def create_task(
        self,
        *,
        user_id: str,
        task_text: str,
        mode: str,
        thinking_level: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        now = utc_now_iso()
        request_payload_json = json.dumps(request_payload, ensure_ascii=False)

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, user_id, status, task_text, mode, thinking_level,
                    request_payload_json, created_at, updated_at
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    user_id,
                    task_text,
                    mode,
                    thinking_level,
                    request_payload_json,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_task(task_id) or {
            "task_id": task_id,
            "status": "queued",
            "task": task_text,
            "mode": mode,
            "thinking_level": thinking_level,
        }

    def mark_started(self, task_id: str) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE task_runs
                SET status = 'running',
                    started_at = COALESCE(started_at, ?),
                    pending_question_json = NULL,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (now, now, task_id),
            )
            self._conn.commit()

    def set_waiting_for_user(self, task_id: str, question_payload: dict[str, Any]) -> None:
        now = utc_now_iso()
        question_json = json.dumps(question_payload, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                UPDATE task_runs
                SET status = 'waiting_for_user',
                    pending_question_json = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (question_json, now, task_id),
            )
            self._conn.commit()

    def resume_task(self, task_id: str) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE task_runs
                SET status = 'running',
                    pending_question_json = NULL,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (now, task_id),
            )
            self._conn.commit()

    def set_route_info(
        self,
        task_id: str,
        *,
        route_name: str,
        route_reason: str,
        route_confidence: float,
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE task_runs
                SET route_name = ?,
                    route_reason = ?,
                    route_confidence = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (route_name, route_reason, route_confidence, now, task_id),
            )
            self._conn.commit()

    def set_result_text(self, task_id: str, result_text: str) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE task_runs
                SET result_text = ?, error_text = NULL, updated_at = ?
                WHERE task_id = ?
                """,
                (result_text, now, task_id),
            )
            self._conn.commit()

    def set_error_text(self, task_id: str, error_text: str) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE task_runs
                SET error_text = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (error_text, now, task_id),
            )
            self._conn.commit()

    def finish_task(self, task_id: str, status: str) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE task_runs
                SET status = ?, finished_at = ?, pending_question_json = NULL, updated_at = ?
                WHERE task_id = ?
                """,
                (status, now, now, task_id),
            )
            self._conn.commit()

    def append_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        created_at = utc_now_iso()
        payload_json = json.dumps(payload, ensure_ascii=False)

        with self._lock:
            cursor = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM task_events WHERE task_id = ?",
                (task_id,),
            )
            seq = int(cursor.fetchone()[0])
            self._conn.execute(
                """
                INSERT INTO task_events (task_id, seq, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, seq, event_type, payload_json, created_at),
            )
            self._conn.execute(
                "UPDATE task_runs SET updated_at = ? WHERE task_id = ?",
                (created_at, task_id),
            )
            self._conn.commit()

        event = {
            "task_id": task_id,
            "seq": seq,
            "type": event_type,
            "created_at": created_at,
        }
        event.update(payload)
        return event

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            """
            SELECT
                task_id,
                user_id,
                status,
                task_text,
                mode,
                thinking_level,
                route_name,
                route_reason,
                route_confidence,
                request_payload_json,
                pending_question_json,
                result_text,
                error_text,
                created_at,
                started_at,
                finished_at,
                updated_at
            FROM task_runs
            WHERE task_id = ?
            """,
            (task_id,),
        )
        if row is None:
            return None

        last_seq_row = self._fetchone(
            "SELECT COALESCE(MAX(seq), 0) AS last_seq FROM task_events WHERE task_id = ?",
            (task_id,),
        )
        payload = json.loads(row["request_payload_json"])
        pending_question = None
        if row["pending_question_json"]:
            try:
                pending_question = json.loads(row["pending_question_json"])
            except json.JSONDecodeError:
                pending_question = {"content": str(row["pending_question_json"])}
        return {
            "task_id": row["task_id"],
            "user_id": row["user_id"],
            "status": row["status"],
            "task": row["task_text"],
            "mode": row["mode"],
            "thinking_level": row["thinking_level"],
            "route_name": row["route_name"],
            "route_reason": row["route_reason"],
            "route_confidence": row["route_confidence"],
            "file_paths": payload.get("file_paths", []),
            "pending_question": pending_question,
            "result": row["result_text"],
            "error": row["error_text"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "updated_at": row["updated_at"],
            "last_seq": int(last_seq_row["last_seq"]) if last_seq_row else 0,
        }

    def get_events_after(self, task_id: str, after_seq: int) -> list[dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT task_id, seq, event_type, payload_json, created_at
            FROM task_events
            WHERE task_id = ? AND seq > ?
            ORDER BY seq ASC
            """,
            (task_id, after_seq),
        )
        events: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            event = {
                "task_id": row["task_id"],
                "seq": int(row["seq"]),
                "type": row["event_type"],
                "created_at": row["created_at"],
            }
            event.update(payload)
            events.append(event)
        return events

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return cursor.fetchone()

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return list(cursor.fetchall())


class TaskService:
    def __init__(self, db_path: Path, dispatcher: TaskDispatcher = dispatch_task) -> None:
        self._store = TaskRunStore(db_path)
        self._dispatcher = dispatcher
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._pending_replies: dict[str, asyncio.Future[str]] = {}

    @property
    def store(self) -> TaskRunStore:
        return self._store

    async def submit_task(
        self,
        *,
        task_text: str,
        user_id: str,
        mode: str,
        thinking_level: str,
        file_paths: list[str],
    ) -> dict[str, Any]:
        request_payload = {
            "task": task_text,
            "user_id": user_id,
            "mode": mode,
            "thinking_level": thinking_level,
            "file_paths": file_paths,
        }
        snapshot = self._store.create_task(
            user_id=user_id,
            task_text=task_text,
            mode=mode,
            thinking_level=thinking_level,
            request_payload=request_payload,
        )
        background = asyncio.create_task(
            self._execute_task(snapshot["task_id"]),
            name=f"task-runner-{snapshot['task_id']}",
        )
        self._background_tasks.add(background)
        background.add_done_callback(self._background_tasks.discard)
        return snapshot

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self._store.get_task(task_id)

    def get_events_after(self, task_id: str, after_seq: int) -> list[dict[str, Any]]:
        return self._store.get_events_after(task_id, after_seq)

    def subscribe(self, task_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers[task_id].add(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = self._subscribers.get(task_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(task_id, None)

    async def _publish(self, task_id: str, event: dict[str, Any]) -> None:
        subscribers = list(self._subscribers.get(task_id, set()))
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("Dropping live event for %s because subscriber queue is full", task_id)

    async def record_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = self._store.append_event(task_id, event_type, payload)
        await self._publish(task_id, event)
        return event

    async def request_user_input(self, task_id: str, question_payload: dict[str, Any]) -> str:
        snapshot = self._store.get_task(task_id)
        if snapshot is None:
            raise RuntimeError("任务不存在，无法请求用户补充。")

        existing_future = self._pending_replies.get(task_id)
        if existing_future is not None and not existing_future.done():
            raise RuntimeError("任务已经在等待用户补充。")

        content = str(question_payload.get("content", "") or "").strip()
        if not content:
            raise ValueError("追问内容不能为空。")

        payload = {
            "content": content,
            "context": str(question_payload.get("context", "") or "").strip(),
            "placeholder": str(question_payload.get("placeholder", "") or "").strip(),
        }

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending_replies[task_id] = future
        self._store.set_waiting_for_user(task_id, payload)
        await self.record_event(task_id, "question", payload)

        try:
            return await future
        finally:
            self._pending_replies.pop(task_id, None)

    async def reply_to_task(self, task_id: str, answer: str) -> dict[str, Any]:
        snapshot = self._store.get_task(task_id)
        if snapshot is None:
            raise KeyError(task_id)

        answer_text = str(answer or "").strip()
        if not answer_text:
            raise ValueError("回复内容不能为空。")

        if snapshot["status"] != "waiting_for_user":
            raise RuntimeError("任务当前不在等待用户回复。")

        future = self._pending_replies.get(task_id)
        if future is None or future.done():
            raise RuntimeError("任务等待上下文已失效，请重新提交任务。")

        self._store.resume_task(task_id)
        await self.record_event(task_id, "user_reply", {"content": answer_text})
        future.set_result(answer_text)
        return self._store.get_task(task_id) or snapshot

    async def _execute_task(self, task_id: str) -> None:
        snapshot = self._store.get_task(task_id)
        if snapshot is None:
            return

        task_text = snapshot["task"]
        user_id = snapshot["user_id"]
        mode = snapshot["mode"]
        thinking_level = snapshot["thinking_level"]
        file_paths = snapshot.get("file_paths", [])
        execution_task = compose_task_text(task_text, file_paths)
        trace_id = new_trace_id()

        decision = await self._dispatcher(task_text, file_paths, mode, user_id)
        self._store.set_route_info(
            task_id,
            route_name=decision.route_name,
            route_reason=decision.reason,
            route_confidence=decision.confidence,
        )

        self._store.mark_started(task_id)
        await self.record_event(task_id, "start", {"content": f"开始执行: {execution_task}"})
        await self.record_event(
            task_id,
            "step",
            {"content": f"🧭 Route: {decision.route_name} ({decision.reason})"},
        )
        log.info("Task received user=%s task=%s", user_id, task_text[:80])

        async def on_step(step_info: str) -> None:
            event_type, payload = parse_step_payload(step_info)
            await self.record_event(task_id, event_type, payload)

        async def request_user_input(question_payload: dict[str, Any]) -> str:
            return await self.request_user_input(task_id, question_payload)

        interaction_token = set_task_interaction_handler(request_user_input)
        try:
            set_thinking_level(thinking_level)
            result = await decision.executor(execution_task, on_step, user_id=user_id)
            self._store.set_result_text(task_id, result)
            await self.record_event(task_id, "result", {"content": result})
        except Exception as exc:
            error_text = str(exc)
            log.error("Task failed: %s", exc)
            self._store.set_error_text(task_id, error_text)
            await self.record_event(task_id, "error", {"content": error_text})
            summary = monitor.get_summary(trace_id)
            await self.record_event(task_id, "monitoring", {"content": summary})
            self._store.finish_task(task_id, "failed")
            monitor.finalize(trace_id)
            return
        finally:
            reset_task_interaction_handler(interaction_token)

        summary = monitor.get_summary(trace_id)
        await self.record_event(task_id, "monitoring", {"content": summary})
        self._store.finish_task(task_id, "succeeded")
        monitor.finalize(trace_id)


def format_sse(event: dict[str, Any]) -> str:
    return (
        f"id: {event['seq']}\n"
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    )
