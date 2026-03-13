# Multi-Agent PPT Generation System Design

## Background

Target users: university PhD researchers, power company R&D leaders.
Core tasks: research direction surveys, PDF interpretation, PPT report generation.
Requirement: work with scattered materials + web search to produce editable .pptx files.

## Architecture

```
User Task
   ↓
┌─────────────────────────────────────┐
│  Orchestrator Agent (strong model)  │
│  - Understand intent, decompose     │
│  - Generate Storyline / PPT outline │
│  - Final synthesis                  │
└──────────┬──────────────────────────┘
           │ concurrent dispatch
     ┌─────┴──────┐
     ↓            ↓
┌─────────┐  ┌──────────────┐
│ Search  │  │ Doc Analyst  │
│ Agent   │  │ Agent        │
│(fast)   │  │(long-context)│
└────┬────┘  └──────┬───────┘
     └──────┬───────┘
            ↓
┌─────────────────────────────────────┐
│  Content Writer Agent               │
│  - Slide body / charts / notes      │
│  - Output → Slide DSL (JSON)        │
└──────────┬──────────────────────────┘
           ↓
┌─────────────────────────────────────┐
│  PPT Engine (pure code, no LLM)     │
│  ① JSON → reveal.js HTML (preview)  │
│  ② JSON → python-pptx (editable)    │
└─────────────────────────────────────┘
```

## Agent Roles & Models

| Agent | Role | Model Traits |
|---|---|---|
| Orchestrator | Decompose tasks, generate storyline, synthesize | Strong reasoning, long context |
| Search Agent | Web search + browser scraping | Fast, good tool calling |
| Doc Analyst | PDF/file parsing, key info extraction | Long context understanding |
| Content Writer | Write slide content, charts, speaker notes → Slide DSL JSON | Strong writing |

## Model Registry

```python
# models.py
MODELS = {
    "orchestrator": {"model": "gpt-5.4",    "base_url": "...", "api_key": "..."},
    "search":       {"model": "gpt-4o-mini", "base_url": "...", "api_key": "..."},
    "doc_analyst":  {"model": "gpt-5.4",    "base_url": "...", "api_key": "..."},
    "writer":       {"model": "gpt-5.4",    "base_url": "...", "api_key": "..."},
}
```

## Slide DSL (JSON)

```json
{
  "meta": {
    "title": "Title",
    "author": "Author",
    "theme": "academic_blue"
  },
  "slides": [
    {
      "layout": "title_slide",
      "title": "...",
      "subtitle": "..."
    },
    {
      "layout": "content_with_chart",
      "title": "...",
      "body": "...",
      "chart": {
        "type": "bar",
        "data": {"labels": [...], "values": [...]},
        "unit": "GW"
      },
      "speaker_notes": "...",
      "image_prompt": null
    },
    {
      "layout": "content_with_image",
      "title": "...",
      "body": "...",
      "image_prompt": "...",
      "speaker_notes": "..."
    }
  ]
}
```

### Layout Types

- `title_slide` — cover page
- `section_header` — section divider
- `content_only` — text only
- `content_with_chart` — text + chart
- `content_with_image` — text + image
- `two_column` — left/right columns
- `comparison` — comparison page
- `summary` — summary page

## LangGraph Implementation

### Main Graph (Orchestrator)

```
planner → dispatch_node → writer → ppt_engine → END
```

- `planner`: strong model, decomposes task + generates storyline
- `dispatch_node`: asyncio.gather(search_graph(), doc_graph())
- `writer`: receives all materials, outputs Slide DSL JSON
- `ppt_engine`: pure code, JSON → .pptx + reveal.js

### Sub-Graphs

- `search_graph`: search_planner → [web_search, browser_navigate] → loop → summarize
- `doc_graph`: doc_reader → [read_local_file, parse_pdf] → extract key points

## File Structure

```
agents-5952bd6c1b/
├── main.py              # FastAPI + WebSocket + /download endpoint
├── models.py            # Model registry + get_llm(role) factory
├── agents/
│   ├── orchestrator.py  # Main graph: planner → dispatch → writer → engine
│   ├── search_agent.py  # Search sub-graph
│   ├── doc_agent.py     # Document analysis sub-graph
│   └── writer_agent.py  # Content writing node
├── ppt_engine/
│   ├── dsl_schema.py    # Slide DSL Pydantic models
│   ├── renderer.py      # JSON → python-pptx
│   └── templates/       # .pptx template files
├── static/              # Frontend (existing)
└── outputs/             # Generated .pptx files
```

## WebSocket Protocol Extension

```json
{"type": "step",   "content": "🔍 Search Agent: searching..."}
{"type": "step",   "content": "📄 Doc Agent: parsing paper1.pdf..."}
{"type": "step",   "content": "✍️ Generating slide content..."}
{"type": "file",   "url": "/download/xxx.pptx", "name": "Report.pptx"}
{"type": "result", "content": "PPT generated, 12 slides. Download above."}
```

## main.py Changes

Add download endpoint:

```python
@app.get("/download/{filename}")
async def download_file(filename: str):
    path = Path("outputs") / filename
    return FileResponse(path, filename=filename)
```

## Key Decisions

1. 4 Agents with independent models per role
2. Slide DSL JSON as the decoupling layer between LLM and rendering
3. python-pptx export for natively editable .pptx files
4. Concurrent sub-graphs for Search + Doc (asyncio.gather)
5. WebSocket real-time progress (reuse existing architecture)
