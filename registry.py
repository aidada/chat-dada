"""
Unified Capability Registry — every agent, tool, and renderer registers here.
Orchestrator dispatches by registry lookup. Zero hardcoded flows.
"""
import asyncio
import importlib
import json
from typing import Any, Callable

from langchain_core.tools import StructuredTool


# Each entry: {fn_path, type, description, input_schema, output_schema, available_to}
REGISTRY: dict[str, dict[str, Any]] = {}


def register(
    name: str,
    *,
    fn_path: str,
    cap_type: str,
    description: str,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    available_to: list[str] | None = None,
):
    """Register a capability (agent, tool, or renderer)."""
    REGISTRY[name] = {
        "fn_path": fn_path,
        "type": cap_type,
        "description": description,
        "input_schema": input_schema or {},
        "output_schema": output_schema or {},
        "available_to": available_to or [],
    }


def get_capability(name: str) -> dict:
    """Look up a registered capability by name."""
    if name not in REGISTRY:
        raise KeyError(f"Capability '{name}' not found in registry. Available: {list(REGISTRY.keys())}")
    return REGISTRY[name]


def resolve_fn(name: str) -> Callable:
    """Resolve capability name to its actual async function."""
    entry = get_capability(name)
    module_path, fn_name = entry["fn_path"].rsplit(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, fn_name)


def list_capabilities(cap_type: str | None = None) -> list[dict]:
    """List all registered capabilities, optionally filtered by type."""
    result = []
    for name, entry in REGISTRY.items():
        if cap_type and entry["type"] != cap_type:
            continue
        result.append({"name": name, **entry})
    return result


def registry_summary() -> str:
    """Generate a text summary of all capabilities for LLM context."""
    lines = []
    for cap_type in ("agent", "tool", "renderer"):
        caps = list_capabilities(cap_type)
        if caps:
            lines.append(f"\n## {cap_type.title()}s")
            for c in caps:
                lines.append(f"- **{c['name']}**: {c['description']}")
    return "\n".join(lines)


def _wrap_as_langchain_tool(name: str, entry: dict) -> StructuredTool:
    """Wrap a registry tool (async def run(input_data) -> dict) as a LangChain StructuredTool."""
    fn = resolve_fn(name)

    async def _invoke(input_text: str) -> str:
        result = await fn(input_text)
        if isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False)
        return str(result)

    return StructuredTool.from_function(
        coroutine=_invoke,
        name=name,
        description=entry["description"],
    )


def get_tools_for_agent(agent_name: str, exclude_names: set[str] | None = None) -> list:
    """Return LangChain tools that are available to the given agent.

    Iterates registry for entries with type=="tool" whose available_to
    includes agent_name. Wraps each as a StructuredTool.
    exclude_names skips tools the agent already defines natively.
    """
    exclude = exclude_names or set()
    tools = []
    for name, entry in REGISTRY.items():
        if entry["type"] != "tool":
            continue
        if agent_name not in entry.get("available_to", []):
            continue
        if name in exclude:
            continue
        tools.append(_wrap_as_langchain_tool(name, entry))
    return tools


# ── Register existing capabilities ──

# Agents
register("search", fn_path="agents.search_agent:run_search",
         cap_type="agent", description="Web search + browser scraping, returns structured findings")
register("doc_analyst", fn_path="agents.doc_agent:run_doc_analysis",
         cap_type="agent", description="Read PDF/text files, extract key info and data")
register("writer", fn_path="agents.writer_agent:run_writer",
         cap_type="agent", description="Generate Slide DSL JSON from storyline and materials")

# Renderers
register("ppt_render", fn_path="ppt_engine.renderer:render_pptx",
         cap_type="renderer", description="Render SlideDeck JSON to editable .pptx file")

# General Chat
register("general_chat", fn_path="agents.general_chat:run",
         cap_type="agent", description="Direct Q&A conversation, answers questions without tools")

# Tools
register("web_search", fn_path="tools.web_search:run",
         cap_type="tool", description="Search the web via Tavily and return results",
         available_to=[])
register("translator", fn_path="tools.translator:run",
         cap_type="tool", description="Translate text to a target language via LLM",
         available_to=["deep_research"])
register("summarizer", fn_path="tools.summarizer:run",
         cap_type="tool", description="Summarize text into key points via LLM",
         available_to=["deep_research", "data_analyst"])
register("code_executor", fn_path="tools.code_executor:run",
         cap_type="tool", description="Execute Python code in a sandboxed subprocess",
         available_to=["deep_research"])
register("academic_search", fn_path="tools.academic_search:run",
         cap_type="tool", description="Search Semantic Scholar and arXiv for academic papers",
         available_to=[])
register("image_gen", fn_path="tools.image_gen:run",
         cap_type="tool", description="Generate images from text prompts via Nano Banana2 API",
         available_to=["deep_research", "data_analyst"])
register("image_to_diagram", fn_path="tools.image_to_diagram:run",
         cap_type="tool", description="Convert image to structured diagram JSON via vision model",
         available_to=["doc_analyst", "deep_research"])

# New Agents (V2)
register("deep_research", fn_path="agents.deep_research:run",
         cap_type="agent", description="Multi-round deep research with web + academic search")
register("data_analyst", fn_path="agents.data_analyst:run",
         cap_type="agent", description="Analyze data files with code execution and generate insights")

# New Renderers (V2)
register("word_render", fn_path="renderers.word_renderer:run",
         cap_type="renderer", description="Render markdown text to editable .docx file")
register("excel_render", fn_path="renderers.excel_renderer:run",
         cap_type="renderer", description="Render structured data to .xlsx Excel file")
register("visio_render", fn_path="renderers.visio_renderer:run",
         cap_type="renderer", description="Render diagram JSON to Visio format (placeholder, outputs JSON)")
