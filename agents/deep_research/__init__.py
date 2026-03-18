"""Deep Research Agent — multi-round research with web search + academic search."""
from agents.deep_research.run import run, CORE_TOOLS, web_search, academic_search, browser_navigate, ask_user_clarification  # noqa: F401
from agents.deep_research.config import (  # noqa: F401
    ResearchConfig,
    ResearchState,
    CHECKPOINT_INTERVAL,
    SUMMARY_INTERVAL,
    DEFAULT_REPORT_PROFILE,
    ACADEMIC_PAPER_GUIDANCE_PROFILE,
    ReportProfile,
    REPORT_PROFILES,
    REPORT_PROFILE_ALIASES,
    ACADEMIC_PROFILE_KEYWORDS,
)
from agents.deep_research.prompts import (  # noqa: F401
    BASE_RESEARCH_SYSTEM,
    BASE_FINAL_REPORT_SYSTEM,
    _build_research_messages,
    _build_research_system,
    _build_final_report_system,
    _normalize_report_profile,
    _get_report_profile,
    _looks_like_academic_paper_task,
    _resolve_report_profile,
)
from agents.deep_research.utils import (  # noqa: F401
    _retry_async,
    _synthesize_parallel_findings,
    _generate_structured_summary,
    _rewrite_final_report,
    _message_text,
    _latest_tool_messages,
)
from agents.deep_research.graphs import (  # noqa: F401
    research_planner,
    research_tools,
    research_finish,
    research_should_continue,
    build_research_graph,
    build_hierarchical_research_graph,
    build_parallel_research_graph,
)

# Re-export for patching: tests patch "agents.deep_research.get_llm" etc.
from core.models import get_llm, get_browser_use_llm  # noqa: F401
from capabilities.memory import ResearchMemory  # noqa: F401
