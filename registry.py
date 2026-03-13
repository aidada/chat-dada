"""
Unified Capability Registry — every agent, tool, and renderer registers here.
Orchestrator dispatches by registry lookup. Zero hardcoded flows.
"""
import importlib
from typing import Any, Callable


# Each entry: {fn_path, type, description, input_schema, output_schema}
REGISTRY: dict[str, dict[str, Any]] = {}


def register(
    name: str,
    *,
    fn_path: str,
    cap_type: str,
    description: str,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
):
    """Register a capability (agent, tool, or renderer)."""
    REGISTRY[name] = {
        "fn_path": fn_path,
        "type": cap_type,
        "description": description,
        "input_schema": input_schema or {},
        "output_schema": output_schema or {},
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
         cap_type="tool", description="Search the web via Tavily and return results")
register("translator", fn_path="tools.translator:run",
         cap_type="tool", description="Translate text to a target language via LLM")
register("summarizer", fn_path="tools.summarizer:run",
         cap_type="tool", description="Summarize text into key points via LLM")
register("code_executor", fn_path="tools.code_executor:run",
         cap_type="tool", description="Execute Python code in a sandboxed subprocess")
register("academic_search", fn_path="tools.academic_search:run",
         cap_type="tool", description="Search Semantic Scholar and arXiv for academic papers")
