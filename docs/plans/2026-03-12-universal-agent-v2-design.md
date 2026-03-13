# Universal Agent System V2 Design

## Background

Upgrade from PPT-only multi-agent system to a general-purpose agent platform inspired by Perplexity Computer. Target users: university PhD researchers, power company R&D leaders. Support arbitrary task types: research, reports, data analysis, image generation, format conversion, and more.

## Architecture

```
User Task (any type)
       ↓
┌──────────────────────────────────────────┐
│  Orchestrator (strong reasoning model)   │
│                                          │
│  1. Intent classification → match        │
│     template OR free-form planning       │
│  2. Output execution plan (JSON):        │
│     steps with parallel_with/depends_on  │
│  3. Schedule agents/tools/renderers      │
│  4. Collect results, return to user      │
└──────────────────────────────────────────┘
           ↓ dynamic dispatch
┌─────────────────────────────────────────────┐
│  Registry (agents + tools + renderers)      │
│  Each entry: {fn, type, description,        │
│               input_schema, output_schema}  │
└─────────────────────────────────────────────┘
```

### Intent Classification + Template Fallback

```python
TEMPLATES = {
    "research_report":   ["deep_research", "writer", "word_render"],
    "ppt_report":        ["deep_research", "doc_analyst", "writer", "image_gen", "ppt_render"],
    "data_analysis":     ["doc_analyst", "data_analyst", "writer"],
    "literature_review": ["deep_research", "doc_analyst", "writer", "word_render"],
    "quick_question":    ["general_chat"],
    "translate_doc":     ["doc_analyst", "translator", "word_render"],
    "image_to_visio":    ["image_to_diagram", "visio_render"],
}
# Unrecognized intent → LLM free-form planning with full registry
```

## Three-Layer Capability Model

### Agents (have LLM + tools + decision loops)

| Agent | Role | Model | Tools Used |
|-------|------|-------|------------|
| deep_research | Multi-round research + academic search | search model | web_search, academic_search, browser |
| doc_analyst | Read files, extract key info | doc model | read_text, read_pdf |
| data_analyst | Analyze data, generate insights | analyst model | code_executor, read files |
| browser_agent | General web automation | search model | browser_use |
| writer | Content writing (reports, slides, papers) | writer model | — |
| general_chat | Direct Q&A conversation | orchestrator model | — |

### Tools (single-call functions, no decision loop)

| Tool | Function | API/Library |
|------|----------|-------------|
| image_gen | Generate images from prompts | Nano Banana2 API |
| translator | Translate text | LLM single call |
| summarizer | Quick summary | LLM single call |
| code_executor | Run Python code in sandbox | subprocess |
| image_to_diagram | Vision model → structured diagram JSON | Vision model |
| file_converter | Format conversion | pandoc / libraries |
| academic_search | Search Semantic Scholar + arXiv | Free APIs |
| web_search | Search via Tavily | Tavily API |

### Renderers (pure code, no LLM)

| Renderer | Input | Output | Library |
|----------|-------|--------|---------|
| ppt_render | Slide DSL JSON | .pptx | python-pptx |
| word_render | Markdown | .docx | python-docx |
| excel_render | Sheets JSON | .xlsx | openpyxl |
| visio_render | Diagram JSON | .vsdx | python-vsdx |

## Execution Plan Format

```json
{
  "intent": "ppt_report",
  "template": "ppt_report",
  "steps": [
    {"id": 1, "type": "agent", "name": "deep_research",
     "input": {"query": "储能技术 2024", "academic": true},
     "parallel_with": [2]},
    {"id": 2, "type": "agent", "name": "doc_analyst",
     "input": {"file_paths": ["paper1.pdf"]},
     "parallel_with": [1]},
    {"id": 3, "type": "agent", "name": "writer",
     "input": {"outline": "auto", "format": "slide_dsl"},
     "depends_on": [1, 2]},
    {"id": 4, "type": "tool", "name": "image_gen",
     "input": {"prompts": "from_step_3"},
     "depends_on": [3]},
    {"id": 5, "type": "renderer", "name": "ppt_render",
     "input": {"slide_dsl": "from_step_3", "images": "from_step_4"},
     "depends_on": [3, 4]}
  ]
}
```

## Unified Capability Interface

```python
# Every agent/tool/renderer follows this contract:
async def run_xxx(input: dict, on_step: Callable | None = None) -> dict:
    """
    Args:
        input: capability-specific input dict
        on_step: optional progress callback
    Returns:
        {"status": "ok", "result": ..., "files": [...]}
    """
```

## File Structure

```
project/
├── models.py                    # Model registry (existing)
├── main.py                      # FastAPI + WebSocket (existing, rewire import)
│
├── orchestrator/                # General-purpose orchestrator (rewrite)
│   ├── __init__.py
│   ├── planner.py               # Intent classification + plan generation
│   ├── scheduler.py             # Dependency graph scheduler
│   └── templates.py             # Preset task templates
│
├── agents/                      # Agents with LLM decision loops
│   ├── __init__.py
│   ├── deep_research.py         # Multi-round search + academic
│   ├── doc_analyst.py           # Document analysis (migrate from existing)
│   ├── data_analyst.py          # Data analysis + code execution
│   ├── browser_agent.py         # General browser automation
│   ├── writer.py                # Content writing (migrate + extend)
│   └── general_chat.py          # Direct conversation
│
├── tools/                       # Single-call tool functions
│   ├── __init__.py
│   ├── image_gen.py             # Nano Banana2 API
│   ├── translator.py            # LLM translation
│   ├── summarizer.py            # LLM summarization
│   ├── code_executor.py         # Python code execution
│   ├── image_to_diagram.py      # Vision → structured diagram
│   ├── file_converter.py        # Format conversion
│   ├── academic_search.py       # Semantic Scholar + arXiv
│   └── web_search.py            # Tavily search
│
├── renderers/                   # Pure code renderers
│   ├── __init__.py
│   ├── schemas.py               # DSL schemas (Slide, Sheet, Diagram)
│   ├── ppt_renderer.py          # JSON → .pptx (migrate from existing)
│   ├── word_renderer.py         # Markdown → .docx
│   ├── excel_renderer.py        # JSON → .xlsx
│   └── visio_renderer.py        # JSON → .vsdx
│
├── registry.py                  # Unified capability registry
├── outputs/                     # Generated files
└── old/                         # Old code (backup)
    ├── agent.py
    ├── agents/
    └── ppt_engine/
```

## Registry Design

```python
# registry.py
REGISTRY = {
    # Agents
    "deep_research":   {"fn": "agents.deep_research:run",   "type": "agent",    "description": "..."},
    "doc_analyst":     {"fn": "agents.doc_analyst:run",      "type": "agent",    "description": "..."},
    "data_analyst":    {"fn": "agents.data_analyst:run",     "type": "agent",    "description": "..."},
    "browser_agent":   {"fn": "agents.browser_agent:run",    "type": "agent",    "description": "..."},
    "writer":          {"fn": "agents.writer:run",           "type": "agent",    "description": "..."},
    "general_chat":    {"fn": "agents.general_chat:run",     "type": "agent",    "description": "..."},
    # Tools
    "image_gen":       {"fn": "tools.image_gen:run",         "type": "tool",     "description": "..."},
    "translator":      {"fn": "tools.translator:run",        "type": "tool",     "description": "..."},
    "summarizer":      {"fn": "tools.summarizer:run",        "type": "tool",     "description": "..."},
    "code_executor":   {"fn": "tools.code_executor:run",     "type": "tool",     "description": "..."},
    "image_to_diagram":{"fn": "tools.image_to_diagram:run",  "type": "tool",     "description": "..."},
    "file_converter":  {"fn": "tools.file_converter:run",    "type": "tool",     "description": "..."},
    "academic_search": {"fn": "tools.academic_search:run",   "type": "tool",     "description": "..."},
    "web_search":      {"fn": "tools.web_search:run",        "type": "tool",     "description": "..."},
    # Renderers
    "ppt_render":      {"fn": "renderers.ppt_renderer:run",  "type": "renderer", "description": "..."},
    "word_render":     {"fn": "renderers.word_renderer:run",  "type": "renderer", "description": "..."},
    "excel_render":    {"fn": "renderers.excel_renderer:run", "type": "renderer", "description": "..."},
    "visio_render":    {"fn": "renderers.visio_renderer:run", "type": "renderer", "description": "..."},
}
```

## Implementation Phases

### Phase 1: Core Infrastructure (rewrite orchestrator + migrate existing)
- orchestrator/ (planner, scheduler, templates)
- registry.py
- Migrate existing agents and ppt_engine to new structure
- Verify everything still works

### Phase 2: New Agents
- deep_research (upgrade search_agent with multi-round + academic_search)
- data_analyst
- general_chat

### Phase 3: New Tools
- academic_search (Semantic Scholar + arXiv)
- image_gen (Nano Banana2)
- translator, summarizer
- code_executor
- image_to_diagram

### Phase 4: New Renderers
- word_renderer
- excel_renderer
- visio_renderer

### Phase 5: Polish
- file_converter
- browser_agent (upgrade from search-only to general)
- Error recovery, retry logic
- WebSocket progress reporting per-step

## Key Design Decisions

1. **Three-layer model** — Agent (LLM loops) / Tool (single call) / Renderer (pure code)
2. **Registry-driven** — Orchestrator dispatches by registry lookup, zero hardcoded flows
3. **Hybrid routing** — Template match for known intents, LLM planning for unknown
4. **Dependency graph scheduling** — parallel_with + depends_on for concurrent execution
5. **Unified interface** — Every capability: `async def run(input, on_step) -> dict`
6. **Extensible** — Add new capability = 1 file + 1 registry entry
