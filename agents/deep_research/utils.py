"""
Deep Research Agent — utility functions (retry, synthesis, rewriting, message helpers).
"""
import asyncio
import logging

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage

from capabilities.context_manager import ResearchContext
from capabilities.progress_tracker import ProgressTracker
from core.content_utils import extract_text_content, normalize_markdown_report
from core.models import get_llm, response_text

from agents.deep_research.config import DEFAULT_REPORT_PROFILE
from agents.deep_research.prompts import _build_final_report_system, _get_report_profile

log = logging.getLogger("chatdada.agent")


async def _retry_async(coro_fn, *args, max_retries: int = 2, delay: float = 1.0, **kwargs):
    """重试异步函数调用，仅对可恢复错误重试。

    可恢复错误：OSError, TimeoutError, ConnectionError
    不可恢复错误：ValueError, TypeError, KeyError → 直接抛出
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except (OSError, TimeoutError, ConnectionError) as e:
            last_exc = e
            if attempt < max_retries:
                await asyncio.sleep(delay * (attempt + 1))
                log.info("Retrying %s (attempt %d/%d)", coro_fn.__name__, attempt + 2, max_retries + 1)
            continue
        except Exception:
            raise
    raise last_exc


async def _generate_structured_summary(query: str, ctx: ResearchContext, tracker: ProgressTracker) -> str:
    """Generate a structured summary of research progress using the orchestrator LLM."""
    llm = get_llm("orchestrator")

    # Collect entry content (each ≤500 chars, max 20 entries)
    entry_texts: list[str] = []
    for entry in ctx.entries[-20:]:
        content = entry.raw_content or entry.compact_content
        if content:
            entry_texts.append(content[:500])

    all_entries = "\n---\n".join(entry_texts) if entry_texts else "(无内容)"

    system_prompt = (
        "你是一个研究进度总结器。请根据提供的研究条目内容，生成一份结构化的研究摘要，包含：\n"
        "1. 已覆盖子主题\n"
        "2. 证据强度评估\n"
        "3. 尚未覆盖的缺口\n"
        "4. 核心发现\n"
        "5. 下一步建议\n\n"
        "总结必须≤800字。"
    )
    human_prompt = (
        f"研究主题：{query}\n\n"
        f"已完成搜索：{', '.join(tracker.completed_searches[-10:]) or '(无)'}\n\n"
        f"研究条目内容：\n{all_entries}"
    )

    resp = await _retry_async(llm.ainvoke, [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prompt),
    ])
    return response_text(resp)


async def _synthesize_parallel_findings(
    query: str,
    subtask_results: dict[str, str],
    report_profile: str,
) -> str:
    """用 orchestrator LLM 合并多个子任务的发现。

    - 去重重叠内容
    - 标注矛盾发现
    - 按报告模板组织结构
    - 输出≤3000字
    """
    llm = get_llm("orchestrator")

    entries = []
    for sid, findings in subtask_results.items():
        entries.append(f"## 子任务 {sid}\n{findings[:1500]}")
    all_entries = "\n\n---\n\n".join(entries)

    profile = _get_report_profile(report_profile)
    sections = "\n".join(f"- {s}" for s in profile.final_sections)

    system_prompt = (
        "你是一个研究合成器。请把多个子任务的研究发现合并成一份结构化报告。\n\n"
        "要求：\n"
        "1. 去除重复内容，保留信息量最大的表述\n"
        '2. 如果子任务之间有矛盾发现，明确标注"[矛盾]"并列出各方证据\n'
        "3. 按以下报告结构组织：\n"
        f"{sections}\n"
        "4. 合并后总长度≤3000字\n"
        "5. 每个结论保留来源标注"
    )

    resp = await _retry_async(llm.ainvoke, [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"研究主题：{query}\n\n子任务发现：\n{all_entries}"),
    ])
    return response_text(resp)


async def _rewrite_final_report(
    query: str,
    findings: str,
    report_profile: str = DEFAULT_REPORT_PROFILE,
) -> str:
    llm = get_llm("deep_research")
    messages = [
        SystemMessage(content=_build_final_report_system(report_profile)),
        HumanMessage(
            content=(
                f"用户问题：{query}\n\n"
                f"当前输出模板：{_get_report_profile(report_profile).name}\n\n"
                "请把下面的研究笔记改写成最终报告。只能基于这些笔记重写，不要新增事实。\n\n"
                f"研究笔记：\n{findings}"
            )
        ),
    ]
    try:
        response = await llm.ainvoke(messages)
    except Exception:
        return normalize_markdown_report(findings)
    return normalize_markdown_report(extract_text_content(response) or findings)


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content)


def _latest_tool_messages(messages: list[BaseMessage]) -> list[ToolMessage]:
    trailing: list[ToolMessage] = []
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            trailing.append(message)
            continue
        break
    trailing.reverse()
    return trailing
