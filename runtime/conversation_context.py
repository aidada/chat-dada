"""
Conversation Context Builder — injects conversation history into LLM prompts.

Three-stage progressive strategy based on conversation length:
- ≤5 rounds: full raw text (zero overhead)
- 6-20 rounds: rolling summary + recent 3 rounds raw
- >20 rounds: rolling summary + recent 3 rounds raw + pgvector retrieval top-3
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from core.models import get_llm, response_text
from infra.db.repositories.conversation_repo import ConversationRepository
from infra.db.repositories.task_event_repo import TaskEventRepository
from infra.db.repositories.task_repo import TaskRunRepository
from infra.db.session import SessionFactory
from langchain_core.messages import HumanMessage, SystemMessage

log = logging.getLogger("chatdada.context")

MAX_CONTEXT_TOKENS = 8000
SUMMARY_MAX_CHARS = 4500  # ~3K tokens
RECENT_ROUNDS = 3
RETRIEVAL_TOP_K = 3
CONTENT_TRUNCATE_CHARS = 400

REFERENTIAL_MARKERS = {
    "刚才", "之前", "上面", "上次", "那个", "这个",
    "继续", "接着", "结合", "综合", "对比", "类似",
    "还是", "同样", "也", "再",
    "it", "that", "previous", "earlier", "above",
}

SUMMARY_PROMPT = """请将以下对话历史压缩为简洁摘要，保留：
1. 每个话题的核心结论
2. 关键数据和事实
3. 用户的具体需求和偏好
删除：寒暄、重复内容、中间推理过程。
控制在 500 字以内。

[现有摘要]
{existing_summary}

[新增对话]
{new_entries}"""


@dataclass
class ConversationContext:
    """Container for built conversation context."""

    text: str = ""
    round_count: int = 0
    strategy: str = "none"  # none, raw, summary, summary+retrieval


@dataclass
class _Round:
    """A conversation round (one user query + one assistant response)."""

    task_id: str
    user_query: str = ""
    assistant_reply: str = ""


def _truncate(text: str, max_chars: int = CONTENT_TRUNCATE_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~1.5 chars per token for mixed CJK/English."""
    return int(len(text) / 1.5)


def _needs_retrieval(message: str) -> bool:
    """Check if the message references earlier conversation content."""
    if len(message) < 15:
        return True
    msg_lower = message.lower()
    return any(marker in msg_lower for marker in REFERENTIAL_MARKERS)


async def _get_conversation_rounds(pool: asyncpg.Pool, conversation_id: str) -> list[_Round]:
    """Fetch all conversation rounds (user query + result) ordered chronologically."""
    rows = await pool.fetch(
        """
        SELECT t.task_id, t.task_text,
               (SELECT e.payload->>'content'
                FROM task_events e
                WHERE e.task_id = t.task_id AND e.event_type = 'result'
                ORDER BY e.seq DESC LIMIT 1) AS result_content
        FROM task_runs t
        WHERE t.conversation_id = $1
          AND t.status IN ('succeeded', 'failed', 'running', 'queued')
        ORDER BY t.created_at ASC
        """,
        conversation_id,
    )
    rounds: list[_Round] = []
    for row in rows:
        rounds.append(
            _Round(
                task_id=row["task_id"],
                user_query=row["task_text"] or "",
                assistant_reply=row["result_content"] or "",
            )
        )
    return rounds


async def _get_conversation_rounds_via_repo(conversation_id: str) -> list[_Round]:
    async with SessionFactory() as session:
        repo = ConversationRepository(session)
        rows = await repo.get_rounds(conversation_id)
    return [
        _Round(
            task_id=row["task_id"],
            user_query=row["task_text"] or "",
            assistant_reply=row["result_content"] or "",
        )
        for row in rows
    ]


def _format_rounds_raw(rounds: list[_Round]) -> str:
    """Format rounds as raw conversation text."""
    parts: list[str] = []
    for r in rounds:
        if r.user_query:
            parts.append(f"Q: {_truncate(r.user_query)}")
        if r.assistant_reply:
            parts.append(f"A: {_truncate(r.assistant_reply)}")
    return "\n\n".join(parts)


async def _generate_summary(existing_summary: str, new_entries_text: str) -> str:
    """Call LLM to merge existing summary + new entries into a new summary."""
    llm = get_llm("orchestrator", temperature=0)
    prompt = SUMMARY_PROMPT.format(
        existing_summary=existing_summary or "（无）",
        new_entries=new_entries_text,
    )
    messages = [
        SystemMessage(content=prompt),
        HumanMessage(content="请生成合并后的摘要。"),
    ]
    response = await llm.ainvoke(messages)
    summary = response_text(response).strip()
    # Enforce max length
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[: SUMMARY_MAX_CHARS - 1] + "…"
    return summary


async def _embed_text(text: str) -> list[float] | None:
    """Generate embedding using Gemini Embedding 2 (1536 dimensions)."""
    try:
        from google import genai

        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            log.warning("GEMINI_API_KEY not set, skipping embedding")
            return None

        client = genai.Client(api_key=api_key)
        result = await asyncio.to_thread(
            client.models.embed_content,
            model="gemini-embedding-2-preview",
            contents=text[:8000],
            config={"output_dimensionality": 1536},
        )
        return list(result.embeddings[0].values)
    except Exception as exc:
        log.warning("Embedding generation failed: %s", exc)
        return None


async def _retrieve_relevant(
    pool: asyncpg.Pool,
    conversation_id: str,
    query_embedding: list[float],
    exclude_task_ids: set[str],
    top_k: int = RETRIEVAL_TOP_K,
) -> list[dict[str, str]]:
    """Retrieve most relevant historical events via pgvector cosine similarity."""
    embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
    rows = await pool.fetch(
        """
        SELECT t.task_text, e.event_type, e.payload->>'content' AS content,
               e.embedding <=> $1::vector AS distance
        FROM task_events e
        JOIN task_runs t ON t.task_id = e.task_id
        WHERE t.conversation_id = $2
          AND e.embedding IS NOT NULL
          AND e.event_type IN ('result', 'error')
        ORDER BY e.embedding <=> $1::vector
        LIMIT $3
        """,
        embedding_str,
        conversation_id,
        top_k + len(exclude_task_ids),  # fetch extra to filter
    )
    results: list[dict[str, str]] = []
    for row in rows:
        if len(results) >= top_k:
            break
        content = row["content"] or ""
        if not content.strip():
            continue
        results.append({
            "query": _truncate(row["task_text"] or "", 200),
            "reply": _truncate(content, 650),
        })
    return results


class ConversationContextBuilder:
    """Builds conversation context for LLM prompt injection."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def build(self, conversation_id: str, current_message: str) -> ConversationContext:
        """Build conversation context string for prompt injection.

        Returns a ConversationContext with .text ready for injection.
        """
        if not conversation_id:
            return ConversationContext()

        if isinstance(self._pool, asyncpg.Pool):
            rounds = await _get_conversation_rounds_via_repo(conversation_id)
        else:
            rounds = await _get_conversation_rounds(self._pool, conversation_id)
        # Exclude the current round (last task that hasn't finished yet)
        # Only consider rounds with a reply as completed rounds
        completed = [r for r in rounds if r.assistant_reply]
        round_count = len(completed)

        if round_count == 0:
            return ConversationContext(round_count=0, strategy="none")

        if round_count <= 5:
            return self._build_raw(completed)

        if round_count <= 20:
            return await self._build_with_summary(conversation_id, completed)

        return await self._build_with_summary_and_retrieval(
            conversation_id, completed, current_message
        )

    def _build_raw(self, rounds: list[_Round]) -> ConversationContext:
        """Stage 1: ≤5 rounds — full raw text."""
        text = f"[对话历史]\n{_format_rounds_raw(rounds)}"
        text = self._truncate_to_budget(text)
        return ConversationContext(
            text=text, round_count=len(rounds), strategy="raw"
        )

    async def _build_with_summary(
        self, conversation_id: str, rounds: list[_Round]
    ) -> ConversationContext:
        """Stage 2: 6-20 rounds — summary + recent raw."""
        recent = rounds[-RECENT_ROUNDS:]
        older = rounds[:-RECENT_ROUNDS]

        summary = await self._get_or_update_summary(conversation_id, older)
        recent_text = _format_rounds_raw(recent)

        parts = [f"[对话历史摘要]\n{summary}", f"[最近对话]\n{recent_text}"]
        text = self._truncate_to_budget("\n\n".join(parts))
        return ConversationContext(
            text=text, round_count=len(rounds), strategy="summary"
        )

    async def _build_with_summary_and_retrieval(
        self,
        conversation_id: str,
        rounds: list[_Round],
        current_message: str,
    ) -> ConversationContext:
        """Stage 3: >20 rounds — summary + recent raw + optional vector retrieval."""
        recent = rounds[-RECENT_ROUNDS:]
        older = rounds[:-RECENT_ROUNDS]
        recent_task_ids = {r.task_id for r in recent}

        summary = await self._get_or_update_summary(conversation_id, older)
        recent_text = _format_rounds_raw(recent)

        parts = [f"[对话历史摘要]\n{summary}", f"[最近对话]\n{recent_text}"]

        # Only do retrieval if message seems referential
        if _needs_retrieval(current_message):
            retrieval_results = await self._try_retrieve(
                conversation_id, current_message, recent_task_ids
            )
            if retrieval_results:
                retrieval_text = self._format_retrieval(retrieval_results)
                parts.insert(1, f"[相关历史片段]\n{retrieval_text}")

        text = self._truncate_to_budget("\n\n".join(parts))
        return ConversationContext(
            text=text, round_count=len(rounds), strategy="summary+retrieval"
        )

    async def _get_or_update_summary(
        self, conversation_id: str, rounds_to_summarize: list[_Round]
    ) -> str:
        """Get cached summary or generate a new one incrementally."""
        from runtime.task_runtime import TaskRunStore

        if isinstance(self._pool, asyncpg.Pool):
            async with SessionFactory() as session:
                repo = ConversationRepository(session)
                existing_summary, through_seq = await repo.get_summary(conversation_id)
        else:
            row = await self._pool.fetchrow(
                "SELECT context_summary, summary_through_seq FROM conversations WHERE id = $1",
                conversation_id,
            )
            existing_summary = (row["context_summary"] or "") if row else ""
            through_seq = (row["summary_through_seq"] or 0) if row else 0

        if not rounds_to_summarize:
            return existing_summary

        # Figure out how many rounds are new (not yet summarized)
        # Use a simple heuristic: if we have N rounds and through_seq covers some,
        # the new rounds are those beyond what's been summarized
        total_older = len(rounds_to_summarize)

        # Estimate how many rounds through_seq covers
        # Each round produces ~2-4 events, so through_seq / 3 is a rough estimate
        estimated_summarized = through_seq // 3 if through_seq > 0 else 0
        new_count = max(0, total_older - estimated_summarized)

        if new_count < 3 and existing_summary:
            # Not enough new rounds to warrant re-summarizing
            return existing_summary

        # Take the new rounds for incremental summarization
        new_rounds = rounds_to_summarize[-new_count:] if new_count > 0 else rounds_to_summarize
        new_entries_text = _format_rounds_raw(new_rounds)

        try:
            summary = await _generate_summary(existing_summary, new_entries_text)
        except Exception as exc:
            log.warning("Summary generation failed: %s", exc)
            return existing_summary

        # Cache the summary
        # Estimate new through_seq based on total rounds
        new_through_seq = total_older * 3  # rough estimate
        try:
            if isinstance(self._pool, asyncpg.Pool):
                async with SessionFactory() as session:
                    repo = ConversationRepository(session)
                    await repo.update_summary(conversation_id, summary, new_through_seq)
                    await session.commit()
            else:
                await self._pool.execute(
                    """
                    UPDATE conversations
                    SET context_summary = $1, summary_through_seq = $2, updated_at = now()
                    WHERE id = $3
                    """,
                    summary,
                    new_through_seq,
                    conversation_id,
                )
        except Exception as exc:
            log.warning("Failed to cache summary: %s", exc)

        return summary

    async def _try_retrieve(
        self,
        conversation_id: str,
        query: str,
        exclude_task_ids: set[str],
    ) -> list[dict[str, str]]:
        """Attempt vector retrieval, return empty list on any failure."""
        try:
            embedding = await _embed_text(query)
            if embedding is None:
                return []
            return await _retrieve_relevant(
                self._pool, conversation_id, embedding, exclude_task_ids
            )
        except Exception as exc:
            log.warning("Vector retrieval failed: %s", exc)
            return []

    def _format_retrieval(self, results: list[dict[str, str]]) -> str:
        parts: list[str] = []
        for r in results:
            parts.append(f"Q: {r['query']}\nA: {r['reply']}")
        return "\n\n".join(parts)

    def _truncate_to_budget(self, text: str) -> str:
        """Truncate text to fit within MAX_CONTEXT_TOKENS budget."""
        estimated = _estimate_tokens(text)
        if estimated <= MAX_CONTEXT_TOKENS:
            return text
        # Truncate to approximate character limit
        max_chars = int(MAX_CONTEXT_TOKENS * 1.5)
        return text[:max_chars - 1] + "…"


async def generate_embeddings_async(pool: asyncpg.Pool, task_id: str) -> None:
    """Generate embeddings for a completed task's events (fire-and-forget).

    Called after task completion to enable future vector retrieval.
    """
    try:
        if isinstance(pool, asyncpg.Pool):
            async with SessionFactory() as session:
                event_repo = TaskEventRepository(session)
                task_repo = TaskRunRepository(session)
                rows = await event_repo.list_pending_embeddings(task_id=task_id)
                user_query = await task_repo.get_task_text(task_id)
                if not rows:
                    return

                for row in rows:
                    payload = dict(row.payload or {})
                    content = str(payload.get("content", "") or "")
                    if not content.strip():
                        continue

                    text_to_embed = content
                    if row.event_type == "result" and user_query:
                        text_to_embed = f"问题: {user_query[:200]}\n回答: {content[:600]}"

                    embedding = await _embed_text(text_to_embed[:2000])
                    if embedding is None:
                        continue

                    embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
                    await event_repo.set_embedding(task_id=row.task_id, seq=row.seq, embedding=embedding_str)

                await session.commit()
                log.info("Generated embeddings for task %s (%d events)", task_id, len(rows))
                return

        rows = await pool.fetch(
            """
            SELECT task_id, seq, event_type, payload->>'content' AS content
            FROM task_events
            WHERE task_id = $1
              AND event_type IN ('result', 'error')
              AND embedding IS NULL
            ORDER BY seq ASC
            """,
            task_id,
        )
        if not rows:
            return

        # Also get the user query text for a combined embedding
        task_row = await pool.fetchrow(
            "SELECT task_text FROM task_runs WHERE task_id = $1", task_id
        )
        user_query = (task_row["task_text"] or "") if task_row else ""

        for row in rows:
            content = row["content"] or ""
            if not content.strip():
                continue

            # For result events, embed the Q+A together for better retrieval
            text_to_embed = content
            if row["event_type"] == "result" and user_query:
                text_to_embed = f"问题: {user_query[:200]}\n回答: {content[:600]}"

            embedding = await _embed_text(text_to_embed[:2000])
            if embedding is None:
                continue

            embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
            await pool.execute(
                """
                UPDATE task_events
                SET embedding = $1::vector
                WHERE task_id = $2 AND seq = $3
                """,
                embedding_str,
                row["task_id"],
                row["seq"],
            )

        log.info("Generated embeddings for task %s (%d events)", task_id, len(rows))
    except Exception as exc:
        log.warning("Embedding generation failed for task %s: %s", task_id, exc)
