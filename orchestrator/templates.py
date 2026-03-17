"""
Preset task templates — known intent types map to predefined execution plans.
Unknown intents fall through to LLM free-form planning.
"""

TEMPLATES = {
    "ppt_report": {
        "description": "Research a topic and generate a PPT report",
        "steps": [
            {"id": 1, "type": "agent", "name": "search",
             "input_key": "search_query", "parallel_with": [2]},
            {"id": 2, "type": "agent", "name": "doc_analyst",
             "input_key": "file_paths", "parallel_with": [1]},
            {"id": 3, "type": "agent", "name": "writer",
             "input_key": "writer_input", "depends_on": [1, 2]},
            {"id": 4, "type": "renderer", "name": "ppt_render",
             "input_key": "render_input", "depends_on": [3]},
        ],
    },
    "research_report": {
        "description": "Deep research on a topic and produce a Markdown report",
        "steps": [
            {"id": 1, "type": "agent", "name": "deep_research",
             "input_key": "search_query", "parallel_with": [2]},
            {"id": 2, "type": "agent", "name": "doc_analyst",
             "input_key": "file_paths", "parallel_with": [1]},
            {"id": 3, "type": "renderer", "name": "markdown_render",
             "input_key": "render_input", "depends_on": [1, 2]},
        ],
    },
    "data_analysis": {
        "description": "Analyze data files and produce insights",
        "steps": [
            {"id": 1, "type": "agent", "name": "doc_analyst",
             "input_key": "file_paths"},
            {"id": 2, "type": "agent", "name": "data_analyst",
             "input_key": "analysis_input", "depends_on": [1]},
            {"id": 3, "type": "agent", "name": "writer",
             "input_key": "writer_input", "depends_on": [2]},
        ],
    },
    "quick_question": {
        "description": "Direct Q&A conversation, no file or search needed",
        "steps": [
            {"id": 1, "type": "agent", "name": "general_chat",
             "input_key": "chat_input"},
        ],
    },
    "translate_doc": {
        "description": "Read a document and translate it",
        "steps": [
            {"id": 1, "type": "agent", "name": "doc_analyst",
             "input_key": "file_paths"},
            {"id": 2, "type": "tool", "name": "translator",
             "input_key": "translate_input", "depends_on": [1]},
            {"id": 3, "type": "renderer", "name": "word_render",
             "input_key": "render_input", "depends_on": [2]},
        ],
    },
    "image_to_visio": {
        "description": "Convert an image to a diagram file",
        "steps": [
            {"id": 1, "type": "tool", "name": "image_to_diagram",
             "input_key": "image_path"},
            {"id": 2, "type": "renderer", "name": "visio_render",
             "input_key": "render_input", "depends_on": [1]},
        ],
    },
    "image_generation": {
        "description": "Generate images from text descriptions",
        "steps": [
            {"id": 1, "type": "tool", "name": "image_gen",
             "input_key": "prompt"},
        ],
    },
}


def get_template(intent: str) -> dict | None:
    """Return template for a known intent, or None for free-form planning."""
    return TEMPLATES.get(intent)


def list_intents() -> list[str]:
    """Return all known intent types."""
    return list(TEMPLATES.keys())


def intent_descriptions() -> str:
    """Text summary of all templates for LLM context."""
    lines = []
    for name, tmpl in TEMPLATES.items():
        steps = " → ".join(s["name"] for s in tmpl["steps"])
        lines.append(f"- **{name}**: {tmpl['description']} ({steps})")
    return "\n".join(lines)
