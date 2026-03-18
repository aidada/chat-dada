"""
Deep Research Agent — entry point (run) and tool definitions.
"""
import logging
import uuid

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from capabilities.context_manager import ResearchContext
from capabilities.memory import ResearchMemory
from core.content_utils import extract_result_text
from core.logger import log_async
from core.models import get_browser_use_llm
from runtime.task_interaction import ask_user
from tools.research_notes import save_research_note, recall_research_notes

from agents.deep_research.config import DEFAULT_REPORT_PROFILE, ResearchConfig
from agents.deep_research.prompts import _resolve_report_profile
from agents.deep_research.utils import _rewrite_final_report

log = logging.getLogger("chatdada.agent")

try:
    from langchain_community.tools.tavily_search import TavilySearchResults
    HAS_TAVILY = True
except ImportError:
    HAS_TAVILY = False


@tool
async def web_search(query: str) -> str:
    """用 Tavily 搜索互联网，适合研究型查询和提取较完整的摘要。"""
    if HAS_TAVILY:
        search = TavilySearchResults(max_results=5)
        results = await search.ainvoke(query)
        return "\n\n".join(f"[{r['url']}]\n{r['content']}" for r in results)
    return f"(TAVILY_API_KEY not configured, skipping '{query}')"


@tool
async def academic_search(query: str) -> str:
    """搜索学术论文（Semantic Scholar + arXiv）。"""
    from tools.academic_search import run as search_academic
    result = await search_academic({"query": query})
    return result.get("result", "No results")


@tool
async def browser_navigate(task_description: str) -> str:
    """控制浏览器完成复杂网页任务。"""
    from browser_use import Agent as BrowserAgent
    from browser_use import BrowserSession as Browser
    from browser_use import BrowserProfile as BrowserConfig
    browser = Browser(browser_profile=BrowserConfig(headless=True))
    llm = get_browser_use_llm("deep_research")
    agent = BrowserAgent(task=task_description, llm=llm, browser=browser, max_actions_per_step=5)
    result = await agent.run(max_steps=10)
    final = result.final_result() if hasattr(result, "final_result") else str(result)
    return final or "Browser task done."


@tool
async def ask_user_clarification(
    question: str,
    why: str = "",
    placeholder: str = "",
) -> str:
    """向用户确认研究方向或判断标准。仅当歧义会显著影响检索方向时调用，且最多调用一次。"""
    answer = await ask_user(question, context=why, placeholder=placeholder)
    if answer is None:
        return "用户交互当前不可用，请基于现有任务描述继续研究。"
    return answer


CORE_TOOLS = [web_search, academic_search, browser_navigate, ask_user_clarification,
              save_research_note, recall_research_notes]


@log_async("agent", "deep_research")
async def run(input_data) -> dict:
    from agents.deep_research.graphs import build_research_graph, build_hierarchical_research_graph, build_parallel_research_graph

    if isinstance(input_data, str):
        query = input_data
        memory_context = ""
        requested_report_profile = ""
    elif isinstance(input_data, dict):
        raw_query = input_data.get("query", input_data.get("search_query", str(input_data)))
        if isinstance(raw_query, dict):
            query = str(
                raw_query.get("query")
                or raw_query.get("task")
                or raw_query.get("search_query")
                or raw_query
            )
            requested_report_profile = str(
                input_data.get("report_profile")
                or raw_query.get("report_profile")
                or ""
            )
        else:
            query = str(raw_query)
            requested_report_profile = str(input_data.get("report_profile", "") or "")
        memory_context = input_data.get("memory_context", "")
    else:
        query = str(input_data)
        memory_context = ""
        requested_report_profile = ""

    report_profile = _resolve_report_profile(query, requested_report_profile)

    # --- input validation ---
    if not query or not query.strip():
        return {"status": "error", "result": "研究查询不能为空。"}
    query = query.strip()
    if len(query) > 10000:
        query = query[:10000]
        log.warning("query truncated to 10000 chars")
    if memory_context and len(memory_context) > 50000:
        memory_context = memory_context[:50000]
        log.warning("memory_context truncated to 50000 chars")

    # --- parse config ---
    config = ResearchConfig()
    if isinstance(input_data, dict) and input_data.get("config"):
        config = ResearchConfig.from_dict(input_data["config"])

    # --- check for resume from checkpoint ---
    resume_task_id = ""
    if isinstance(input_data, dict):
        resume_task_id = input_data.get("resume_task_id", "")

    resumed_state: dict | None = None
    if resume_task_id:
        try:
            memory = ResearchMemory(resume_task_id)
            checkpoint = memory.load_checkpoint()
            if checkpoint:
                meta = memory.load_meta()
                old_query = (meta or {}).get("query", "")
                # Detect query mismatch on resume
                if query and query != str(input_data) and old_query and query != old_query:
                    log.warning("Resume query mismatch: checkpoint='%s', new='%s'. Using new query.",
                                old_query[:50], query[:50])
                elif not query or query == str(input_data):
                    query = old_query or query
                if not requested_report_profile:
                    report_profile = (meta or {}).get("report_profile", report_profile)
                resumed_state = {
                    "step_count": checkpoint.get("step_count", 0),
                    "research_context": checkpoint.get("research_context", {}),
                    "progress": checkpoint.get("progress", {}),
                }
                log.info("Resumed research from checkpoint for task %s at step %d", resume_task_id, resumed_state["step_count"])
        except Exception:
            log.warning("Failed to resume from checkpoint %s, starting fresh", resume_task_id, exc_info=True)

    # --- initialize external memory ---
    if resume_task_id and resumed_state:
        task_id = resume_task_id
    else:
        task_id = f"research_{uuid.uuid4().hex[:12]}"
        try:
            ResearchMemory(task_id).init(query, report_profile)
        except Exception:
            log.warning("research_memory init failed for %s", task_id, exc_info=True)
            task_id = ""

    graph = build_research_graph(config)
    # --- select graph mode ---
    use_hierarchical = isinstance(input_data, dict) and input_data.get("hierarchical", False)
    use_parallel = isinstance(input_data, dict) and input_data.get("parallel", False)
    if use_parallel:
        graph = build_parallel_research_graph()
    elif use_hierarchical:
        graph = build_hierarchical_research_graph()
    task_prompt = f"请深入研究以下主题：\n{query}"
    if memory_context:
        task_prompt = f"{memory_context}\n\n{task_prompt}"
    state = {
        "messages": [HumanMessage(content=task_prompt)],
        "query": query,
        "step_count": resumed_state["step_count"] if resumed_state else 0,
        "report_profile": report_profile,
        "research_context": resumed_state["research_context"] if resumed_state else {},
        "task_id": task_id,
        "progress": resumed_state["progress"] if resumed_state else {},
        "research_plan": {},
        "current_subtask": {},
    }
    result = await graph.ainvoke(state)
    # Extract final text from result
    final_text = result.get("_final_text", "")
    if not final_text:
        ctx = ResearchContext.from_dict(result.get("research_context", {}))
        final_text = ctx.build_final_context()
    final_text = extract_result_text(final_text)
    if final_text:
        final_text = await _rewrite_final_report(query, final_text, report_profile)

    # --- persist final report ---
    if task_id:
        try:
            ResearchMemory(task_id).save_final_report(final_text)
        except Exception:
            log.warning("research_memory save_final_report failed", exc_info=True)

    return {"status": "ok", "result": final_text}
