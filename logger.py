"""
Logging & Monitoring — structured logging, trace propagation, and real-time monitoring.

Components:
  - Trace ID via ContextVar (async-safe per-request ID)
  - Verbose mode toggle (runtime switch for input/output previews)
  - setup_logging() — colored console + file rotation
  - MonitoringCollector — event collection for WebSocket push
  - @log_async decorator — auto-instrument any async function
  - _LoggingLLM — transparent proxy that logs all LLM calls
"""

import functools
import logging
import time
import uuid
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage

# ── 1. Trace ID ──────────────────────────────────────────────────────────────

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def new_trace_id() -> str:
    """Generate a new trace ID and set it in the current async context."""
    tid = uuid.uuid4().hex[:8]
    _trace_id.set(tid)
    return tid


def get_trace_id() -> str:
    return _trace_id.get()


# ── 2. Verbose mode ─────────────────────────────────────────────────────────

_global_verbose: bool = False


def set_verbose(enabled: bool) -> None:
    global _global_verbose
    _global_verbose = enabled


def is_verbose() -> bool:
    return _global_verbose


# ── 3. Logging setup ────────────────────────────────────────────────────────

_LEVEL_COLORS = {
    "DEBUG":    "\033[36m",   # cyan
    "INFO":     "\033[32m",   # green
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[1;31m", # bold red
}
_RESET = "\033[0m"


class ColoredFormatter(logging.Formatter):
    """Console formatter with ANSI colors and trace_id prefix."""

    def format(self, record: logging.LogRecord) -> str:
        tid = get_trace_id()
        prefix = f"[{tid}] " if tid else ""
        color = _LEVEL_COLORS.get(record.levelname, "")
        record.msg = f"{color}{prefix}{record.levelname:<7}{_RESET} {record.msg}"
        return super().format(record)


class PlainFormatter(logging.Formatter):
    """File formatter with trace_id, no colors."""

    def format(self, record: logging.LogRecord) -> str:
        tid = get_trace_id()
        prefix = f"[{tid}] " if tid else ""
        record.msg = f"{prefix}{record.levelname:<7} {record.msg}"
        return super().format(record)


_console_handler: logging.StreamHandler | None = None


def setup_logging(level: int = logging.INFO) -> None:
    """Configure console (colored) + file (rotating) logging."""
    global _console_handler

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console
    _console_handler = logging.StreamHandler()
    _console_handler.setLevel(level)
    _console_handler.setFormatter(ColoredFormatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(_console_handler)

    # File
    fh = TimedRotatingFileHandler(
        str(log_dir / "app.log"), when="midnight", backupCount=7, encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(PlainFormatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)

    # Reduce uvicorn access noise
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def set_log_level(level: str) -> None:
    """Change console log level at runtime. Accepts 'DEBUG', 'INFO', etc."""
    if _console_handler:
        _console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))


# ── 4. MonitoringCollector ───────────────────────────────────────────────────

@dataclass
class MonitoringEvent:
    timestamp: float
    trace_id: str
    layer: str       # "orchestrator" | "agent" | "tool" | "llm"
    name: str
    event: str       # "start" | "end" | "error"
    duration_ms: float | None = None
    metadata: dict = field(default_factory=dict)


class MonitoringCollector:
    """Collects monitoring events, grouped by trace_id."""

    def __init__(self, history_limit: int = 50) -> None:
        self._requests: dict[str, list[MonitoringEvent]] = {}
        self._history: deque[dict] = deque(maxlen=history_limit)

    def record(self, event: MonitoringEvent) -> None:
        self._requests.setdefault(event.trace_id, []).append(event)

    def get_summary(self, trace_id: str) -> dict:
        events = self._requests.get(trace_id, [])
        total_duration = 0.0
        llm_count = 0
        total_tokens = 0
        error_count = 0

        for ev in events:
            if ev.event == "end" and ev.duration_ms:
                if ev.layer == "orchestrator" and ev.name == "run_orchestrator":
                    total_duration = ev.duration_ms
            if ev.layer == "llm" and ev.event == "end":
                llm_count += 1
                total_tokens += ev.metadata.get("tokens", 0)
            if ev.event == "error":
                error_count += 1

        if not total_duration and events:
            starts = [e for e in events if e.event == "start"]
            ends = [e for e in events if e.event in ("end", "error")]
            if starts and ends:
                total_duration = (ends[-1].timestamp - starts[0].timestamp) * 1000

        return {
            "trace_id": trace_id,
            "total_duration_ms": round(total_duration, 1),
            "llm_call_count": llm_count,
            "total_tokens": total_tokens,
            "error_count": error_count,
            "events": [
                {
                    "layer": e.layer,
                    "name": e.name,
                    "event": e.event,
                    "duration_ms": round(e.duration_ms, 1) if e.duration_ms else None,
                    "metadata": e.metadata,
                }
                for e in events
            ],
        }

    def finalize(self, trace_id: str) -> None:
        summary = self.get_summary(trace_id)
        self._history.append(summary)
        self._requests.pop(trace_id, None)

    def get_history(self) -> list[dict]:
        return list(self._history)


monitor = MonitoringCollector()


# ── 5. @log_async decorator ─────────────────────────────────────────────────

def _preview(obj: Any, limit: int = 200) -> str:
    """Truncated string preview of any object."""
    s = str(obj)
    return s[:limit] + "..." if len(s) > limit else s


_DEFAULT_RESPONSES_INSTRUCTIONS = "You are a helpful AI assistant."


def _extract_message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


def _extract_llm_content(obj: Any) -> str:
    content = getattr(obj, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    text = getattr(obj, "text", None)
    if text is not None:
        return str(text)
    return str(content)


def _prepare_responses_kwargs(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if kwargs.get("instructions"):
        return args, kwargs

    if not args:
        return args, {**kwargs, "instructions": _DEFAULT_RESPONSES_INSTRUCTIONS}

    first_arg = args[0]
    if not isinstance(first_arg, list) or not all(isinstance(msg, BaseMessage) for msg in first_arg):
        return args, {**kwargs, "instructions": _DEFAULT_RESPONSES_INSTRUCTIONS}

    instruction_parts: list[str] = []
    remaining_messages: list[BaseMessage] = []
    consuming_prefix = True

    for message in first_arg:
        is_instruction_message = isinstance(message, SystemMessage) or (
            getattr(message, "additional_kwargs", {}).get("__openai_role__") == "developer"
        )
        if consuming_prefix and is_instruction_message:
            text = _extract_message_text(message).strip()
            if text:
                instruction_parts.append(text)
            continue
        consuming_prefix = False
        remaining_messages.append(message)

    if not instruction_parts:
        return args, {**kwargs, "instructions": _DEFAULT_RESPONSES_INSTRUCTIONS}

    new_args = (remaining_messages, *args[1:])
    new_kwargs = {**kwargs, "instructions": "\n\n".join(instruction_parts)}
    return new_args, new_kwargs


def log_async(layer: str, name: str):
    """Decorator that logs entry/exit/error for async functions and records MonitoringEvents."""

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            log = logging.getLogger(f"chatdada.{layer}")
            tid = get_trace_id()

            log.info(f"{name} started")
            if is_verbose():
                log.debug(f"{name} args={_preview(args)} kwargs={_preview(kwargs)}")

            monitor.record(MonitoringEvent(
                timestamp=time.time(), trace_id=tid, layer=layer,
                name=name, event="start",
            ))

            t0 = time.time()
            try:
                result = await fn(*args, **kwargs)
                dur = (time.time() - t0) * 1000

                meta: dict[str, Any] = {"duration_ms": round(dur, 1)}
                if is_verbose():
                    meta["output_preview"] = _preview(result)
                    log.debug(f"{name} output={_preview(result)}")

                monitor.record(MonitoringEvent(
                    timestamp=time.time(), trace_id=tid, layer=layer,
                    name=name, event="end", duration_ms=dur, metadata=meta,
                ))
                log.info(f"{name} done ({dur:.0f}ms)")
                return result

            except Exception as exc:
                dur = (time.time() - t0) * 1000
                log.error(f"{name} error ({dur:.0f}ms): {exc}")
                monitor.record(MonitoringEvent(
                    timestamp=time.time(), trace_id=tid, layer=layer,
                    name=name, event="error", duration_ms=dur,
                    metadata={"error": str(exc)},
                ))
                raise

        return wrapper
    return decorator


# ── 6. _LoggingLLM proxy ────────────────────────────────────────────────────

class _LoggingLLM:
    """Transparent proxy that logs ainvoke() calls (tokens, latency, model)."""

    def __init__(self, llm, role: str, model: str) -> None:
        self._llm = llm
        self._role = role
        self._model = model

    async def ainvoke(self, *args, **kwargs):
        log = logging.getLogger("chatdada.llm")
        tid = get_trace_id()
        label = f"LLM({self._role}/{self._model})"

        if getattr(self._llm, "use_responses_api", False):
            args, kwargs = _prepare_responses_kwargs(args, kwargs)

        log.info(f"{label} call started")
        monitor.record(MonitoringEvent(
            timestamp=time.time(), trace_id=tid, layer="llm",
            name=label, event="start",
        ))

        t0 = time.time()
        try:
            result = await self._llm.ainvoke(*args, **kwargs)
            dur = (time.time() - t0) * 1000

            tokens = 0
            if hasattr(result, "usage_metadata") and result.usage_metadata:
                tokens = getattr(result.usage_metadata, "total_tokens", 0) or 0

            meta = {
                "model": self._model,
                "role": self._role,
                "tokens": tokens,
                "duration_ms": round(dur, 1),
            }
            if is_verbose() and hasattr(result, "content"):
                meta["output_preview"] = _preview(result.content)

            monitor.record(MonitoringEvent(
                timestamp=time.time(), trace_id=tid, layer="llm",
                name=label, event="end", duration_ms=dur, metadata=meta,
            ))
            log.info(f"{label} done ({dur:.0f}ms, {tokens} tokens)")
            return result

        except Exception as exc:
            dur = (time.time() - t0) * 1000
            log.error(f"{label} error ({dur:.0f}ms): {exc}")
            monitor.record(MonitoringEvent(
                timestamp=time.time(), trace_id=tid, layer="llm",
                name=label, event="error", duration_ms=dur,
                metadata={"error": str(exc), "model": self._model},
            ))
            raise

    async def astream(self, *args, **kwargs):
        log = logging.getLogger("chatdada.llm")
        tid = get_trace_id()
        label = f"LLM({self._role}/{self._model})"

        if getattr(self._llm, "use_responses_api", False):
            args, kwargs = _prepare_responses_kwargs(args, kwargs)

        log.info(f"{label} stream started")
        monitor.record(MonitoringEvent(
            timestamp=time.time(), trace_id=tid, layer="llm",
            name=label, event="start",
        ))

        t0 = time.time()
        tokens = 0
        preview_parts: list[str] = []

        try:
            async for chunk in self._llm.astream(*args, **kwargs):
                usage = getattr(chunk, "usage_metadata", None)
                if usage:
                    if isinstance(usage, dict):
                        tokens = int(usage.get("total_tokens", tokens) or tokens)
                    else:
                        tokens = int(getattr(usage, "total_tokens", tokens) or tokens)

                if is_verbose() and len("".join(preview_parts)) < 200:
                    chunk_text = _extract_llm_content(chunk)
                    if chunk_text:
                        preview_parts.append(chunk_text)

                yield chunk

            dur = (time.time() - t0) * 1000
            meta = {
                "model": self._model,
                "role": self._role,
                "tokens": tokens,
                "duration_ms": round(dur, 1),
            }
            if is_verbose() and preview_parts:
                meta["output_preview"] = _preview("".join(preview_parts))

            monitor.record(MonitoringEvent(
                timestamp=time.time(), trace_id=tid, layer="llm",
                name=label, event="end", duration_ms=dur, metadata=meta,
            ))
            log.info(f"{label} stream done ({dur:.0f}ms, {tokens} tokens)")

        except Exception as exc:
            dur = (time.time() - t0) * 1000
            log.error(f"{label} stream error ({dur:.0f}ms): {exc}")
            monitor.record(MonitoringEvent(
                timestamp=time.time(), trace_id=tid, layer="llm",
                name=label, event="error", duration_ms=dur,
                metadata={"error": str(exc), "model": self._model},
            ))
            raise

    def bind_tools(self, *args, **kwargs):
        """Return a new _LoggingLLM wrapping the bound LLM (prevents LangGraph from losing the proxy)."""
        bound = self._llm.bind_tools(*args, **kwargs)
        return _LoggingLLM(bound, self._role, self._model)

    def __getattr__(self, name: str):
        """Delegate everything else to the underlying LLM."""
        return getattr(self._llm, name)
