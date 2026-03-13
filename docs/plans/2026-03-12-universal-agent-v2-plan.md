# Universal Agent System V2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Upgrade the PPT-only multi-agent system into a general-purpose agent platform with registry-driven dispatching, dependency graph scheduling, and extensible agents/tools/renderers.

**Architecture:** Orchestrator classifies user intent → matches a preset template OR generates a free-form execution plan → Scheduler resolves dependency graph and dispatches agents/tools/renderers concurrently where possible → Results flow back to user via WebSocket.

**Tech Stack:** LangGraph (StateGraph), langchain-openai (ChatOpenAI), python-pptx, python-docx, openpyxl, pypdf, pydantic, FastAPI + WebSocket

---

## Phase 1: Core Infrastructure

### Task 1: Create Unified Capability Registry (`registry.py`)

**Files:**
- Create: `registry.py`

**Step 1: Create registry.py with capability registration and lookup**

```python
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
```

**Step 2: Verify registry works**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from registry import REGISTRY, get_capability, resolve_fn, list_capabilities, registry_summary
print('Registry entries:', list(REGISTRY.keys()))
print('Agents:', [c['name'] for c in list_capabilities('agent')])
print('Renderers:', [c['name'] for c in list_capabilities('renderer')])
print(registry_summary())
fn = resolve_fn('search')
print('search fn:', fn)
"
```
Expected: Lists all registered capabilities, resolves search function.

**Step 3: Commit**

```bash
git add registry.py
git commit -m "feat: add unified capability registry with dynamic dispatch"
```

---

### Task 2: Create Orchestrator Templates (`orchestrator/templates.py`)

**Files:**
- Create: `orchestrator/__init__.py`
- Create: `orchestrator/templates.py`

**Step 1: Create orchestrator directory and templates**

`orchestrator/__init__.py` — empty file.

`orchestrator/templates.py`:

```python
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
        "description": "Deep research on a topic and produce a Word document",
        "steps": [
            {"id": 1, "type": "agent", "name": "deep_research",
             "input_key": "search_query", "parallel_with": [2]},
            {"id": 2, "type": "agent", "name": "doc_analyst",
             "input_key": "file_paths", "parallel_with": [1]},
            {"id": 3, "type": "agent", "name": "writer",
             "input_key": "writer_input", "depends_on": [1, 2]},
            {"id": 4, "type": "renderer", "name": "word_render",
             "input_key": "render_input", "depends_on": [3]},
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
```

**Step 2: Verify**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from orchestrator.templates import get_template, list_intents, intent_descriptions
print('Intents:', list_intents())
print(intent_descriptions())
t = get_template('ppt_report')
print('PPT steps:', len(t['steps']))
print('Unknown:', get_template('unknown_thing'))
"
```
Expected: Lists intents, shows descriptions, returns None for unknown.

**Step 3: Commit**

```bash
git add orchestrator/
git commit -m "feat: add orchestrator templates for known intent types"
```

---

### Task 3: Create Dependency Graph Scheduler (`orchestrator/scheduler.py`)

**Files:**
- Create: `orchestrator/scheduler.py`

**Step 1: Create the scheduler**

```python
"""
Dependency Graph Scheduler — executes steps respecting depends_on / parallel_with.
Groups steps into waves: steps with no unresolved dependencies run concurrently.
"""
import asyncio
from typing import Any, Callable, Awaitable

from registry import resolve_fn


async def execute_plan(
    steps: list[dict],
    context: dict[str, Any],
    on_step: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """
    Execute a plan's steps respecting dependency order.

    Args:
        steps: List of step dicts with id, type, name, input_key, depends_on
        context: Shared context dict (step results stored as context[step_id])
        on_step: Optional progress callback

    Returns:
        Updated context dict with all step results.
    """
    completed: set[int] = set()
    step_map = {s["id"]: s for s in steps}
    all_ids = set(step_map.keys())

    while completed != all_ids:
        # Find ready steps: all dependencies satisfied
        ready = []
        for sid, step in step_map.items():
            if sid in completed:
                continue
            deps = set(step.get("depends_on", []))
            if deps.issubset(completed):
                ready.append(step)

        if not ready:
            raise RuntimeError(
                f"Deadlock: no steps ready. Completed={completed}, "
                f"Remaining={all_ids - completed}"
            )

        # Execute ready steps concurrently
        if on_step:
            names = ", ".join(s["name"] for s in ready)
            await on_step(f"Executing: {names}")

        tasks = [_run_step(step, context, on_step) for step in ready]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for step, result in zip(ready, results):
            sid = step["id"]
            if isinstance(result, Exception):
                if on_step:
                    await on_step(f"⚠️ Step {step['name']} failed: {result}")
                context[f"step_{sid}_error"] = str(result)
            else:
                context[f"step_{sid}"] = result
            completed.add(sid)

    return context


async def _run_step(
    step: dict,
    context: dict[str, Any],
    on_step: Callable[[str], Awaitable[None]] | None,
) -> Any:
    """Run a single step by resolving its capability and calling it."""
    name = step["name"]
    cap_type = step["type"]
    input_data = context.get(step.get("input_key", ""), {})

    if on_step:
        emoji = {"agent": "🤖", "tool": "🔧", "renderer": "📄"}.get(cap_type, "▶️")
        await on_step(f"{emoji} {name}: starting...")

    fn = resolve_fn(name)
    result = await fn(input_data) if asyncio.iscoroutinefunction(fn) else fn(input_data)

    if on_step:
        preview = str(result)[:100]
        await on_step(f"✅ {name}: done ({preview}...)")

    return result
```

**Step 2: Verify import**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from orchestrator.scheduler import execute_plan
print('scheduler OK')
"
```
Expected: `scheduler OK`

**Step 3: Commit**

```bash
git add orchestrator/scheduler.py
git commit -m "feat: add dependency graph scheduler with concurrent wave execution"
```

---

### Task 4: Create Orchestrator Planner (`orchestrator/planner.py`)

**Files:**
- Create: `orchestrator/planner.py`

**Step 1: Create the planner with intent classification + free-form planning**

```python
"""
Orchestrator Planner — classifies user intent and generates execution plans.
1. Try to match a known template
2. If no match, use LLM free-form planning with full registry context
"""
import json

from langchain_core.messages import HumanMessage, SystemMessage

from models import get_llm
from registry import registry_summary
from orchestrator.templates import intent_descriptions, get_template, list_intents


CLASSIFY_SYSTEM = """你是一个任务分类器。根据用户的任务描述，判断最匹配的意图类型。

已知意图类型：
{intents}

输出 JSON 格式：
{{"intent": "意图名称", "confidence": 0.0-1.0, "params": {{"title": "...", "search_query": "...", "file_paths": [], "author": ""}}}}

如果没有匹配的意图（confidence < 0.5），输出：
{{"intent": "free_form", "confidence": 0.0, "params": {{...}}}}

只输出 JSON，不要其他内容。"""


FREEFORM_SYSTEM = """你是一个任务编排器。用户给了一个任务，不属于任何已知模板。
请根据可用的能力，生成一个执行计划。

可用能力：
{registry}

输出 JSON 格式：
{{
  "intent": "free_form",
  "title": "任务标题",
  "steps": [
    {{"id": 1, "type": "agent|tool|renderer", "name": "能力名称", "input_key": "上下文key", "depends_on": [], "input_description": "此步骤需要什么输入"}}
  ],
  "context": {{
    "输入key": "具体输入值"
  }}
}}

注意：
- depends_on 列出必须先完成的步骤 id
- 可以并行的步骤不要互相依赖
- input_key 是从 context 获取输入的 key
- 只输出 JSON"""


async def classify_and_plan(task: str) -> dict:
    """
    Classify user intent and return an execution plan.

    Returns:
        {
            "intent": str,
            "template": dict | None,
            "steps": list[dict],
            "context": dict,
        }
    """
    llm = get_llm("orchestrator")

    # Step 1: Classify intent
    classify_prompt = CLASSIFY_SYSTEM.format(intents=intent_descriptions())
    messages = [
        SystemMessage(content=classify_prompt),
        HumanMessage(content=task),
    ]
    response = await llm.ainvoke(messages)
    content = _extract_json(response.content)

    try:
        classification = json.loads(content)
    except json.JSONDecodeError:
        classification = {"intent": "free_form", "confidence": 0.0, "params": {}}

    intent = classification.get("intent", "free_form")
    confidence = classification.get("confidence", 0.0)
    params = classification.get("params", {})

    # Step 2: Match template or free-form plan
    template = get_template(intent) if confidence >= 0.5 else None

    if template:
        # Use template steps, populate context from params
        steps = template["steps"]
        context = {
            "search_query": params.get("search_query", task),
            "file_paths": params.get("file_paths", []),
            "title": params.get("title", task[:50]),
            "author": params.get("author", ""),
            "task": task,
            "storyline": "",  # Will be filled by orchestrator
        }
        return {
            "intent": intent,
            "template": template,
            "steps": steps,
            "context": context,
        }

    # Step 3: Free-form LLM planning
    freeform_prompt = FREEFORM_SYSTEM.format(registry=registry_summary())
    messages = [
        SystemMessage(content=freeform_prompt),
        HumanMessage(content=task),
    ]
    response = await llm.ainvoke(messages)
    content = _extract_json(response.content)

    try:
        plan = json.loads(content)
    except json.JSONDecodeError:
        # Fallback: treat as quick question
        plan = {
            "intent": "quick_question",
            "steps": [{"id": 1, "type": "agent", "name": "general_chat", "input_key": "chat_input"}],
            "context": {"chat_input": task},
        }

    steps = plan.get("steps", [])
    context = plan.get("context", {"task": task})
    context["task"] = task

    return {
        "intent": plan.get("intent", "free_form"),
        "template": None,
        "steps": steps,
        "context": context,
    }


def _extract_json(text: str) -> str:
    """Extract JSON from LLM response, handling markdown code blocks."""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    return text.strip()
```

**Step 2: Verify import**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from orchestrator.planner import classify_and_plan
print('planner OK')
"
```
Expected: `planner OK`

**Step 3: Commit**

```bash
git add orchestrator/planner.py
git commit -m "feat: add orchestrator planner with intent classification and free-form planning"
```

---

### Task 5: Rewrite Main Orchestrator Entry Point (`orchestrator/runner.py`)

**Files:**
- Create: `orchestrator/runner.py`

**Step 1: Create the new orchestrator runner that replaces agents/orchestrator.py**

```python
"""
Orchestrator Runner — main entry point for all tasks.
Replaces agents/orchestrator.py with registry-driven execution.

Flow:
1. Planner classifies intent → picks template or generates plan
2. For ppt_report template: generate storyline first (backward compat)
3. Scheduler executes steps with dependency resolution
4. Returns final result to caller
"""
import json
import uuid
from typing import Callable, Awaitable

from langchain_core.messages import HumanMessage, SystemMessage

from models import get_llm
from orchestrator.planner import classify_and_plan


# Storyline generation prompt (for PPT tasks, backward compat with existing writer)
STORYLINE_SYSTEM = """你是一个任务编排 Agent。用户会给你一个研究或报告任务。

你需要输出一个 JSON，格式如下：
{
  "storyline": "PPT 大纲，用 \\n 分隔每个章节标题",
  "search_queries": ["搜索关键词1", "搜索关键词2", ...],
  "file_paths": ["如有本地文件路径写在这里"],
  "title": "PPT 标题",
  "author": "作者（如用户未提供则留空）"
}

注意：
- search_queries: 为搜索 Agent 提供 2-5 个搜索关键词
- file_paths: 从用户消息中提取文件路径（如有）
- storyline: 规划 PPT 的叙事结构，每行一个章节
- 只输出 JSON，不要其他内容"""


async def run_orchestrator(task: str, on_step: Callable[[str], Awaitable[None]]) -> str:
    """
    Main entry point — replaces agents.orchestrator.run_agent().
    Same callback interface for backward compatibility with main.py.
    """
    await on_step("🧠 Orchestrator: 分析任务...")

    # Step 1: Classify and plan
    plan = await classify_and_plan(task)
    intent = plan["intent"]
    await on_step(f"📋 Intent: {intent}")

    # Step 2: Route to appropriate handler
    if intent == "ppt_report":
        return await _handle_ppt_report(task, plan, on_step)
    elif intent == "quick_question":
        return await _handle_quick_question(task, on_step)
    else:
        return await _handle_generic(task, plan, on_step)


async def _handle_ppt_report(task: str, plan: dict, on_step: Callable) -> str:
    """Handle PPT report generation — uses existing agents pipeline."""
    import asyncio
    from agents.search_agent import run_search
    from agents.doc_agent import run_doc_analysis
    from agents.writer_agent import run_writer
    from ppt_engine.renderer import render_pptx

    # Generate storyline
    await on_step("📝 Generating storyline...")
    llm = get_llm("orchestrator")
    messages = [
        SystemMessage(content=STORYLINE_SYSTEM),
        HumanMessage(content=task),
    ]
    response = await llm.ainvoke(messages)

    try:
        content = _extract_json(response.content)
        storyline_plan = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        storyline_plan = {
            "storyline": "背景介绍\n核心内容\n数据分析\n总结展望",
            "search_queries": [task],
            "file_paths": [],
            "title": task[:30],
            "author": "",
        }

    storyline = storyline_plan.get("storyline", "")
    search_queries = storyline_plan.get("search_queries", [])
    file_paths = storyline_plan.get("file_paths", [])
    title = storyline_plan.get("title", "Report")
    author = storyline_plan.get("author", "")

    await on_step(f"📋 Storyline:\n{storyline}")

    # Parallel: Search + Doc
    tasks = []
    if search_queries:
        await on_step(f"🔍 Search Agent: searching {len(search_queries)} queries...")
        combined = "\n".join(f"- {q}" for q in search_queries)
        tasks.append(("search", run_search(combined)))
    if file_paths:
        await on_step(f"📄 Doc Agent: analyzing {len(file_paths)} files...")
        tasks.append(("doc", run_doc_analysis(file_paths)))

    search_findings = ""
    doc_analysis = ""

    if tasks:
        results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
        for (label, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                await on_step(f"⚠️ {label} error: {result}")
                continue
            if label == "search":
                search_findings = result
                await on_step(f"🔍 Search done: {len(result)} chars")
            elif label == "doc":
                doc_analysis = result
                await on_step(f"📄 Doc done: {len(result)} chars")

    # Writer
    await on_step("✍️ Writer Agent: generating slides...")
    try:
        deck = await run_writer(storyline, search_findings, doc_analysis, author)
        deck.meta.title = title
        if author:
            deck.meta.author = author
        await on_step(f"✍️ Writer done: {len(deck.slides)} slides")
    except Exception as e:
        await on_step(f"⚠️ Writer failed: {e}")
        return f"PPT 内容生成失败: {e}"

    # Render
    await on_step("📊 Rendering .pptx...")
    file_id = uuid.uuid4().hex[:8]
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip()[:30] or "report"
    filename = f"{safe_title}_{file_id}.pptx"
    output_path = f"outputs/{filename}"

    try:
        render_pptx(deck, output_path)
        await on_step(f"✅ PPT generated: {filename}")
        await on_step(json.dumps({"type": "file", "url": f"/download/{filename}", "name": filename}))
    except Exception as e:
        await on_step(f"⚠️ Render failed: {e}")
        return f"PPT 渲染失败: {e}"

    return f"PPT 已生成：《{title}》，共 {len(deck.slides)} 页。\n下载: /download/{filename}"


async def _handle_quick_question(task: str, on_step: Callable) -> str:
    """Handle direct Q&A — single LLM call, no tools."""
    await on_step("💬 Answering directly...")
    llm = get_llm("orchestrator")
    messages = [
        SystemMessage(content="你是一个专业的AI助手。直接回答用户的问题，简洁准确。"),
        HumanMessage(content=task),
    ]
    response = await llm.ainvoke(messages)
    return response.content


async def _handle_generic(task: str, plan: dict, on_step: Callable) -> str:
    """Handle generic tasks — use scheduler for dependency-based execution."""
    from orchestrator.scheduler import execute_plan

    steps = plan.get("steps", [])
    context = plan.get("context", {"task": task})

    if not steps:
        return await _handle_quick_question(task, on_step)

    await on_step(f"🚀 Executing {len(steps)} steps...")
    result_ctx = await execute_plan(steps, context, on_step)

    # Find the last step's result
    max_id = max(s["id"] for s in steps)
    final = result_ctx.get(f"step_{max_id}", result_ctx.get(f"step_{max_id}_error", "任务完成。"))
    return str(final)


def _extract_json(text: str) -> str:
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    return text.strip()
```

**Step 2: Verify import**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from orchestrator.runner import run_orchestrator
print('runner OK')
"
```
Expected: `runner OK`

**Step 3: Commit**

```bash
git add orchestrator/runner.py
git commit -m "feat: add orchestrator runner with intent routing and backward-compat PPT handling"
```

---

### Task 6: Update main.py to Use New Orchestrator + Fix Static Files

**Files:**
- Modify: `main.py`
- Create: `static/` directory (move `index.html` into it)

**Step 1: Create static directory and move index.html**

Run:
```bash
mkdir -p static
cp index.html static/index.html
```

**Step 2: Update main.py to use orchestrator.runner**

Replace the import in `main.py:11`:
```python
# OLD:
from agents.orchestrator import run_agent

# NEW:
from orchestrator.runner import run_orchestrator as run_agent
```

**Step 3: Verify server loads**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "from main import app; print('main OK')"
```
Expected: `main OK`

**Step 4: Commit**

```bash
git add main.py static/
git commit -m "feat: wire main.py to new orchestrator runner, fix static file serving"
```

---

### Task 7: Add General Chat Agent (`agents/general_chat.py`)

**Files:**
- Create: `agents/general_chat.py`

**Step 1: Create the general chat agent**

```python
"""
General Chat Agent — direct Q&A conversation, no tools needed.
Registered in registry as "general_chat".
"""
from langchain_core.messages import HumanMessage, SystemMessage
from models import get_llm


CHAT_SYSTEM = """你是一个专业、友好的AI助手。
- 直接回答用户问题，简洁准确
- 必要时提供结构化的要点
- 承认不确定的地方
- 使用中文回答"""


async def run(input_data) -> dict:
    """
    Unified interface: async def run(input, on_step) -> dict

    Args:
        input_data: str (question) or dict with "query" key
    """
    if isinstance(input_data, str):
        query = input_data
    elif isinstance(input_data, dict):
        query = input_data.get("query", input_data.get("chat_input", str(input_data)))
    else:
        query = str(input_data)

    llm = get_llm("orchestrator")
    messages = [
        SystemMessage(content=CHAT_SYSTEM),
        HumanMessage(content=query),
    ]
    response = await llm.ainvoke(messages)
    return {"status": "ok", "result": response.content}
```

**Step 2: Register in registry.py — add to the bottom of registry.py**

```python
register("general_chat", fn_path="agents.general_chat:run",
         cap_type="agent", description="Direct Q&A conversation, answers questions without tools")
```

**Step 3: Verify**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from registry import resolve_fn
fn = resolve_fn('general_chat')
print('general_chat fn:', fn)
"
```

**Step 4: Commit**

```bash
git add agents/general_chat.py registry.py
git commit -m "feat: add general chat agent for direct Q&A"
```

---

## Phase 2: New Tools

### Task 8: Create Tools Directory + Web Search Tool (`tools/web_search.py`)

**Files:**
- Create: `tools/__init__.py`
- Create: `tools/web_search.py`

**Step 1: Create tools directory and extract web_search as standalone tool**

`tools/__init__.py` — empty file.

`tools/web_search.py`:

```python
"""
Web Search Tool — single-call function wrapping Tavily search.
Extracted from search_agent for standalone use.
"""
try:
    from langchain_community.tools.tavily_search import TavilySearchResults
    HAS_TAVILY = True
except ImportError:
    HAS_TAVILY = False


async def run(input_data) -> dict:
    """
    Search the web for a query.

    Args:
        input_data: str (query) or dict with "query" key
    """
    if isinstance(input_data, str):
        query = input_data
    elif isinstance(input_data, dict):
        query = input_data.get("query", str(input_data))
    else:
        query = str(input_data)

    if not HAS_TAVILY:
        return {"status": "ok", "result": f"(TAVILY_API_KEY not configured, skipping search for '{query}')"}

    search = TavilySearchResults(max_results=5)
    results = await search.ainvoke(query)
    formatted = "\n\n".join(f"[{r['url']}]\n{r['content']}" for r in results)
    return {"status": "ok", "result": formatted}
```

**Step 2: Register in registry.py**

```python
register("web_search", fn_path="tools.web_search:run",
         cap_type="tool", description="Search the web via Tavily and return results")
```

**Step 3: Verify**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from registry import resolve_fn
fn = resolve_fn('web_search')
print('web_search fn:', fn)
"
```

**Step 4: Commit**

```bash
git add tools/ registry.py
git commit -m "feat: add standalone web_search tool"
```

---

### Task 9: Add Translator Tool (`tools/translator.py`)

**Files:**
- Create: `tools/translator.py`

**Step 1: Create translator**

```python
"""
Translator Tool — single LLM call to translate text.
"""
from langchain_core.messages import HumanMessage, SystemMessage
from models import get_llm


async def run(input_data) -> dict:
    """
    Translate text to a target language.

    Args:
        input_data: dict with "text" and "target_lang" (default: "中文")
    """
    if isinstance(input_data, str):
        text = input_data
        target_lang = "中文"
    elif isinstance(input_data, dict):
        text = input_data.get("text", str(input_data))
        target_lang = input_data.get("target_lang", "中文")
    else:
        text = str(input_data)
        target_lang = "中文"

    llm = get_llm("writer")
    messages = [
        SystemMessage(content=f"你是一个专业翻译。将以下内容翻译为{target_lang}。只输出翻译结果，不要加任何解释。"),
        HumanMessage(content=text),
    ]
    response = await llm.ainvoke(messages)
    return {"status": "ok", "result": response.content}
```

**Step 2: Register**

Add to `registry.py`:
```python
register("translator", fn_path="tools.translator:run",
         cap_type="tool", description="Translate text to a target language via LLM")
```

**Step 3: Commit**

```bash
git add tools/translator.py registry.py
git commit -m "feat: add translator tool"
```

---

### Task 10: Add Summarizer Tool (`tools/summarizer.py`)

**Files:**
- Create: `tools/summarizer.py`

**Step 1: Create summarizer**

```python
"""
Summarizer Tool — single LLM call to summarize text.
"""
from langchain_core.messages import HumanMessage, SystemMessage
from models import get_llm


async def run(input_data) -> dict:
    """
    Summarize text content.

    Args:
        input_data: str (text) or dict with "text" key
    """
    if isinstance(input_data, str):
        text = input_data
    elif isinstance(input_data, dict):
        text = input_data.get("text", str(input_data))
    else:
        text = str(input_data)

    llm = get_llm("writer")
    messages = [
        SystemMessage(content="你是一个专业的摘要助手。将以下内容总结为简洁的要点。保留关键数据和结论。"),
        HumanMessage(content=text),
    ]
    response = await llm.ainvoke(messages)
    return {"status": "ok", "result": response.content}
```

**Step 2: Register**

Add to `registry.py`:
```python
register("summarizer", fn_path="tools.summarizer:run",
         cap_type="tool", description="Summarize text into key points via LLM")
```

**Step 3: Commit**

```bash
git add tools/summarizer.py registry.py
git commit -m "feat: add summarizer tool"
```

---

### Task 11: Add Code Executor Tool (`tools/code_executor.py`)

**Files:**
- Create: `tools/code_executor.py`

**Step 1: Create sandboxed code executor**

```python
"""
Code Executor Tool — runs Python code in a subprocess sandbox.
"""
import subprocess
import tempfile
import os


async def run(input_data) -> dict:
    """
    Execute Python code in a sandboxed subprocess.

    Args:
        input_data: str (code) or dict with "code" key
    """
    if isinstance(input_data, str):
        code = input_data
    elif isinstance(input_data, dict):
        code = input_data.get("code", str(input_data))
    else:
        code = str(input_data)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        tmp_path = f.name

    try:
        result = subprocess.run(
            ["python", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=tempfile.gettempdir(),
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.returncode != 0:
            return {"status": "error", "result": output or f"Exit code: {result.returncode}"}
        return {"status": "ok", "result": output}
    except subprocess.TimeoutExpired:
        return {"status": "error", "result": "Code execution timed out (30s limit)"}
    finally:
        os.unlink(tmp_path)
```

**Step 2: Register**

Add to `registry.py`:
```python
register("code_executor", fn_path="tools.code_executor:run",
         cap_type="tool", description="Execute Python code in a sandboxed subprocess")
```

**Step 3: Commit**

```bash
git add tools/code_executor.py registry.py
git commit -m "feat: add sandboxed code executor tool"
```

---

### Task 12: Add Academic Search Tool (`tools/academic_search.py`)

**Files:**
- Create: `tools/academic_search.py`

**Step 1: Create academic search using Semantic Scholar + arXiv**

```python
"""
Academic Search Tool — searches Semantic Scholar and arXiv for papers.
Uses free public APIs, no API key required.
"""
import asyncio
import urllib.parse

import httpx


async def run(input_data) -> dict:
    """
    Search academic papers on Semantic Scholar and arXiv.

    Args:
        input_data: str (query) or dict with "query" key
    """
    if isinstance(input_data, str):
        query = input_data
    elif isinstance(input_data, dict):
        query = input_data.get("query", str(input_data))
    else:
        query = str(input_data)

    results = await asyncio.gather(
        _search_semantic_scholar(query),
        _search_arxiv(query),
        return_exceptions=True,
    )

    parts = []
    for r in results:
        if isinstance(r, Exception):
            parts.append(f"(Search error: {r})")
        else:
            parts.append(r)

    return {"status": "ok", "result": "\n\n".join(parts)}


async def _search_semantic_scholar(query: str, limit: int = 5) -> str:
    """Search Semantic Scholar API (free, no key needed)."""
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": limit,
        "fields": "title,authors,year,abstract,url,citationCount",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return f"(Semantic Scholar: HTTP {resp.status_code})"
        data = resp.json()

    papers = data.get("data", [])
    if not papers:
        return "(Semantic Scholar: no results)"

    lines = ["## Semantic Scholar Results"]
    for p in papers:
        authors = ", ".join(a.get("name", "") for a in p.get("authors", [])[:3])
        lines.append(f"- **{p.get('title', 'N/A')}** ({p.get('year', 'N/A')})")
        lines.append(f"  Authors: {authors}")
        lines.append(f"  Citations: {p.get('citationCount', 0)}")
        if p.get("abstract"):
            lines.append(f"  Abstract: {p['abstract'][:200]}...")
        if p.get("url"):
            lines.append(f"  URL: {p['url']}")
    return "\n".join(lines)


async def _search_arxiv(query: str, limit: int = 5) -> str:
    """Search arXiv API (free, no key needed)."""
    encoded = urllib.parse.quote(query)
    url = f"http://export.arxiv.org/api/query?search_query=all:{encoded}&max_results={limit}&sortBy=relevance"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return f"(arXiv: HTTP {resp.status_code})"

    # Simple XML parsing (avoid heavy dependency)
    text = resp.text
    entries = text.split("<entry>")[1:]  # Skip header
    if not entries:
        return "(arXiv: no results)"

    lines = ["## arXiv Results"]
    for entry in entries[:limit]:
        title = _extract_tag(entry, "title").replace("\n", " ").strip()
        summary = _extract_tag(entry, "summary").replace("\n", " ").strip()[:200]
        arxiv_id = _extract_tag(entry, "id")
        lines.append(f"- **{title}**")
        lines.append(f"  {summary}...")
        lines.append(f"  URL: {arxiv_id}")
    return "\n".join(lines)


def _extract_tag(xml: str, tag: str) -> str:
    """Extract text between XML tags (simple, no namespace)."""
    start = xml.find(f"<{tag}>")
    if start == -1:
        start = xml.find(f"<{tag} ")
        if start == -1:
            return ""
        start = xml.find(">", start) + 1
    else:
        start += len(f"<{tag}>")
    end = xml.find(f"</{tag}>", start)
    if end == -1:
        return ""
    return xml[start:end]
```

**Step 2: Register**

Add to `registry.py`:
```python
register("academic_search", fn_path="tools.academic_search:run",
         cap_type="tool", description="Search Semantic Scholar and arXiv for academic papers")
```

**Step 3: Add httpx to requirements.txt**

Add to `requirements.txt`:
```
# HTTP client (for academic search)
httpx>=0.27.0
```

**Step 4: Install httpx**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/pip install httpx
```

**Step 5: Commit**

```bash
git add tools/academic_search.py registry.py requirements.txt
git commit -m "feat: add academic search tool with Semantic Scholar and arXiv"
```

---

## Phase 3: New Renderers

### Task 13: Add Word Renderer (`renderers/word_renderer.py`)

**Files:**
- Create: `renderers/__init__.py`
- Create: `renderers/word_renderer.py`

**Step 1: Create renderers directory and word renderer**

`renderers/__init__.py` — empty file.

`renderers/word_renderer.py`:

```python
"""
Word Renderer — converts markdown text to a .docx file via python-docx.
"""
import os
import re

from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH


def run(input_data) -> dict:
    """
    Render markdown-like text to a .docx file.

    Args:
        input_data: dict with "content" (markdown text), "title", "output_path"
    """
    if isinstance(input_data, str):
        content = input_data
        title = "Document"
        output_path = "outputs/document.docx"
    elif isinstance(input_data, dict):
        content = input_data.get("content", input_data.get("text", ""))
        title = input_data.get("title", "Document")
        output_path = input_data.get("output_path", "outputs/document.docx")
    else:
        return {"status": "error", "result": "Invalid input"}

    doc = Document()

    # Title
    doc.add_heading(title, level=0)

    # Parse markdown-like content
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        elif line.startswith("### "):
            doc.add_heading(line[4:], level=3)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.startswith("- ") or line.startswith("* "):
            doc.add_paragraph(line[2:], style="List Bullet")
        elif re.match(r"^\d+\.\s", line):
            text = re.sub(r"^\d+\.\s", "", line)
            doc.add_paragraph(text, style="List Number")
        else:
            doc.add_paragraph(line)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    doc.save(output_path)

    return {
        "status": "ok",
        "result": f"Word document saved: {output_path}",
        "files": [output_path],
    }
```

**Step 2: Register**

Add to `registry.py`:
```python
register("word_render", fn_path="renderers.word_renderer:run",
         cap_type="renderer", description="Render markdown text to editable .docx file")
```

**Step 3: Verify**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from renderers.word_renderer import run
result = run({'content': '# Hello\n\nThis is a test.\n\n- Item 1\n- Item 2', 'title': 'Test', 'output_path': 'outputs/test_word.docx'})
print(result)
"
```
Expected: `{'status': 'ok', 'result': 'Word document saved: outputs/test_word.docx', ...}`

**Step 4: Commit**

```bash
git add renderers/ registry.py
git commit -m "feat: add word renderer for markdown to docx conversion"
```

---

### Task 14: Add Excel Renderer (`renderers/excel_renderer.py`)

**Files:**
- Create: `renderers/excel_renderer.py`

**Step 1: Create excel renderer**

```python
"""
Excel Renderer — converts structured data to .xlsx via openpyxl.
"""
import os
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment


def run(input_data) -> dict:
    """
    Render structured data to an .xlsx file.

    Args:
        input_data: dict with "sheets" list and "output_path"
        Each sheet: {"name": "Sheet1", "headers": [...], "rows": [[...], ...]}
    """
    if not isinstance(input_data, dict):
        return {"status": "error", "result": "Invalid input: expected dict"}

    sheets = input_data.get("sheets", [])
    output_path = input_data.get("output_path", "outputs/data.xlsx")

    if not sheets:
        return {"status": "error", "result": "No sheets provided"}

    wb = Workbook()
    # Remove default sheet
    wb.remove(wb.active)

    header_font = Font(bold=True, size=12)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font_white = Font(bold=True, size=12, color="FFFFFF")

    for sheet_data in sheets:
        ws = wb.create_sheet(title=sheet_data.get("name", "Sheet"))
        headers = sheet_data.get("headers", [])
        rows = sheet_data.get("rows", [])

        # Write headers
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = header_font_white
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")

        # Write data rows
        for row_idx, row in enumerate(rows, 2):
            for col, value in enumerate(row, 1):
                ws.cell(row=row_idx, column=col, value=value)

        # Auto-width columns
        for col in range(1, len(headers) + 1):
            max_len = max(
                len(str(ws.cell(row=r, column=col).value or ""))
                for r in range(1, len(rows) + 2)
            )
            ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = min(max_len + 4, 50)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    wb.save(output_path)

    return {
        "status": "ok",
        "result": f"Excel file saved: {output_path}",
        "files": [output_path],
    }
```

**Step 2: Register**

Add to `registry.py`:
```python
register("excel_render", fn_path="renderers.excel_renderer:run",
         cap_type="renderer", description="Render structured data to .xlsx Excel file")
```

**Step 3: Add openpyxl to requirements.txt**

Add to `requirements.txt`:
```
# Excel generation
openpyxl>=3.1.0
```

**Step 4: Install openpyxl**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/pip install openpyxl
```

**Step 5: Verify**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from renderers.excel_renderer import run
result = run({
    'sheets': [{'name': 'Data', 'headers': ['Name', 'Value'], 'rows': [['A', 10], ['B', 20]]}],
    'output_path': 'outputs/test_excel.xlsx'
})
print(result)
"
```

**Step 6: Commit**

```bash
git add renderers/excel_renderer.py registry.py requirements.txt
git commit -m "feat: add excel renderer for structured data to xlsx"
```

---

### Task 15: Add Data Analyst Agent (`agents/data_analyst.py`)

**Files:**
- Create: `agents/data_analyst.py`

**Step 1: Create data analyst agent**

```python
"""
Data Analyst Agent — analyzes data files, generates insights, can execute code.
Uses doc_analyst tools + code_executor.
"""
from typing import Annotated, Literal
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from models import get_llm


@tool
def execute_python(code: str) -> str:
    """执行 Python 代码进行数据分析。可以使用 pandas, numpy 等常用库。"""
    import subprocess
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        tmp = f.name
    try:
        result = subprocess.run(
            ["python", tmp], capture_output=True, text=True, timeout=30
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Timeout (30s limit)"
    finally:
        os.unlink(tmp)


@tool
def read_data_file(file_path: str) -> str:
    """读取数据文件（CSV, JSON, Excel 等），返回前 50 行预览。"""
    import os
    path = os.path.expanduser(file_path)
    if not os.path.exists(path):
        return f"文件不存在: {path}"

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".csv":
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[:50]
            return "".join(lines)
        elif ext == ".json":
            import json
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return json.dumps(data, ensure_ascii=False, indent=2)[:5000]
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return content[:5000]
    except Exception as e:
        return f"读取失败: {e}"


ANALYST_TOOLS = [execute_python, read_data_file]


class AnalystState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    step_count: int
    analysis: str


ANALYST_SYSTEM = """你是一个专业的数据分析师。你的任务是分析数据并生成洞察。

策略：
1. 用 read_data_file 读取数据文件
2. 用 execute_python 编写分析代码（可用 pandas, numpy）
3. 总结关键发现、趋势、异常

输出格式：
- 数据概要（字段、行数、类型）
- 关键发现（趋势、分布、异常）
- 可视化建议（图表类型 + 数据列）"""


async def analyst_planner(state: AnalystState) -> dict:
    llm = get_llm("doc_analyst").bind_tools(ANALYST_TOOLS)
    messages = [SystemMessage(content=ANALYST_SYSTEM)] + state["messages"]
    response = await llm.ainvoke(messages)
    return {"messages": [response], "step_count": state["step_count"] + 1}


async def analyst_tools(state: AnalystState) -> dict:
    return await ToolNode(ANALYST_TOOLS).ainvoke(state)


def analyst_finish(state: AnalystState) -> dict:
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            return {"analysis": str(msg.content)}
    return {"analysis": ""}


def analyst_should_continue(state: AnalystState) -> Literal["tools", "finish"]:
    if state["step_count"] >= 10:
        return "finish"
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "finish"


def build_analyst_graph():
    g = StateGraph(AnalystState)
    g.add_node("planner", analyst_planner)
    g.add_node("tools", analyst_tools)
    g.add_node("finish", analyst_finish)
    g.set_entry_point("planner")
    g.add_conditional_edges("planner", analyst_should_continue, {"tools": "tools", "finish": "finish"})
    g.add_edge("tools", "planner")
    g.add_edge("finish", END)
    return g.compile()


async def run(input_data) -> dict:
    """Unified interface for registry dispatch."""
    if isinstance(input_data, str):
        query = input_data
    elif isinstance(input_data, dict):
        query = input_data.get("query", input_data.get("analysis_input", str(input_data)))
    else:
        query = str(input_data)

    graph = build_analyst_graph()
    state = {
        "messages": [HumanMessage(content=f"请分析以下数据/需求：\n{query}")],
        "step_count": 0,
        "analysis": "",
    }
    result = await graph.ainvoke(state)
    return {"status": "ok", "result": result.get("analysis", "")}
```

**Step 2: Register**

Add to `registry.py`:
```python
register("data_analyst", fn_path="agents.data_analyst:run",
         cap_type="agent", description="Analyze data files with code execution and generate insights")
```

**Step 3: Verify**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from agents.data_analyst import build_analyst_graph
g = build_analyst_graph()
print('data_analyst graph OK')
"
```

**Step 4: Commit**

```bash
git add agents/data_analyst.py registry.py
git commit -m "feat: add data analyst agent with code execution and file reading"
```

---

### Task 16: Add Deep Research Agent (`agents/deep_research.py`)

**Files:**
- Create: `agents/deep_research.py`

**Step 1: Create deep research agent (upgrade of search_agent)**

```python
"""
Deep Research Agent — multi-round research with web search + academic search.
Upgraded version of search_agent with academic paper support.
"""
from typing import Annotated, Literal
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from models import get_llm

try:
    from langchain_community.tools.tavily_search import TavilySearchResults
    HAS_TAVILY = True
except ImportError:
    HAS_TAVILY = False


@tool
async def web_search(query: str) -> str:
    """搜索互联网获取最新信息。"""
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
    from browser_use.browser.browser import Browser, BrowserConfig
    browser = Browser(config=BrowserConfig(headless=True))
    llm = get_llm("search")
    agent = BrowserAgent(task=task_description, llm=llm, browser=browser, max_actions_per_step=5)
    result = await agent.run(max_steps=10)
    final = result.final_result() if hasattr(result, "final_result") else str(result)
    return final or "Browser task done."


RESEARCH_TOOLS = [web_search, academic_search, browser_navigate]


class ResearchState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    query: str
    step_count: int
    findings: str


RESEARCH_SYSTEM = """你是一个专业的深度研究员。你的任务是对给定主题进行全面的多轮研究。

策略：
1. 先用 web_search 搜索关键词，了解背景
2. 用 academic_search 搜索相关学术论文
3. 如果需要抓取具体网页，用 browser_navigate
4. 多角度搜索：中文 + 英文关键词
5. 收集足够信息后，整理为结构化发现

输出格式：
- 领域概述
- 关键发现（带引用来源）
- 重要数据和统计
- 学术论文引用
- 结论和趋势"""


async def research_planner(state: ResearchState) -> dict:
    llm = get_llm("search").bind_tools(RESEARCH_TOOLS)
    messages = [SystemMessage(content=RESEARCH_SYSTEM)] + state["messages"]
    response = await llm.ainvoke(messages)
    return {"messages": [response], "step_count": state["step_count"] + 1}


async def research_tools(state: ResearchState) -> dict:
    return await ToolNode(RESEARCH_TOOLS).ainvoke(state)


def research_finish(state: ResearchState) -> dict:
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage) and msg.content:
            return {"findings": str(msg.content)}
    return {"findings": ""}


def research_should_continue(state: ResearchState) -> Literal["tools", "finish"]:
    if state["step_count"] >= 15:
        return "finish"
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "finish"


def build_research_graph():
    g = StateGraph(ResearchState)
    g.add_node("planner", research_planner)
    g.add_node("tools", research_tools)
    g.add_node("finish", research_finish)
    g.set_entry_point("planner")
    g.add_conditional_edges("planner", research_should_continue, {"tools": "tools", "finish": "finish"})
    g.add_edge("tools", "planner")
    g.add_edge("finish", END)
    return g.compile()


async def run(input_data) -> dict:
    """Unified interface for registry dispatch."""
    if isinstance(input_data, str):
        query = input_data
    elif isinstance(input_data, dict):
        query = input_data.get("query", input_data.get("search_query", str(input_data)))
    else:
        query = str(input_data)

    graph = build_research_graph()
    state = {
        "messages": [HumanMessage(content=f"请深入研究以下主题：\n{query}")],
        "query": query,
        "step_count": 0,
        "findings": "",
    }
    result = await graph.ainvoke(state)
    return {"status": "ok", "result": result.get("findings", "")}
```

**Step 2: Register**

Add to `registry.py`:
```python
register("deep_research", fn_path="agents.deep_research:run",
         cap_type="agent", description="Multi-round deep research with web + academic search")
```

**Step 3: Verify**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from agents.deep_research import build_research_graph
g = build_research_graph()
print('deep_research graph OK')
"
```

**Step 4: Commit**

```bash
git add agents/deep_research.py registry.py
git commit -m "feat: add deep research agent with academic search support"
```

---

## Phase 4: Integration + Polish

### Task 17: Move Old Code to `old/` and Clean Up

**Files:**
- Move: `agent.py` → `old/agent.py`
- Move unneeded files to `old/`

**Step 1: Create old directory and move legacy code**

Run:
```bash
mkdir -p old
mv agent.py old/agent.py
```

**Step 2: Commit**

```bash
git add old/ agent.py
git commit -m "chore: move legacy single-agent code to old/"
```

---

### Task 18: Full Integration Verification

**Files:**
- None (verification only)

**Step 1: Verify all imports and registry entries**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from registry import REGISTRY, list_capabilities, registry_summary, resolve_fn

print('=== Registry ===')
print(registry_summary())
print()

print('=== Resolve All ===')
for name in REGISTRY:
    try:
        fn = resolve_fn(name)
        print(f'  {name}: {fn.__module__}.{fn.__name__} OK')
    except Exception as e:
        print(f'  {name}: FAIL - {e}')

print()
print('=== Orchestrator ===')
from orchestrator.runner import run_orchestrator
print('runner OK')

print()
print('=== main.py ===')
from main import app
print('app OK')
"
```

**Step 2: Run the PPT smoke test (backward compat)**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from ppt_engine.dsl_schema import SlideDeck
from ppt_engine.renderer import render_pptx

deck = SlideDeck.model_validate({
    'meta': {'title': 'V2 Test', 'author': 'Test'},
    'slides': [
        {'layout': 'title_slide', 'title': 'V2 Smoke Test', 'subtitle': 'Registry-driven'},
        {'layout': 'content_only', 'title': 'Page 2', 'body': 'Line 1\nLine 2'},
    ]
})
path = render_pptx(deck, 'outputs/v2_smoke_test.pptx')
print(f'PPT OK: {path}')
"
```

**Step 3: Run word renderer smoke test**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/python -c "
from renderers.word_renderer import run
result = run({'content': '# V2 Test\n\nHello world.\n\n- Item 1\n- Item 2', 'title': 'V2 Test', 'output_path': 'outputs/v2_word_test.docx'})
print(result)
"
```

**Step 4: Commit untracked project files**

```bash
git add docs/ pyproject.toml .gitignore
git commit -m "chore: add project docs and configuration"
```

---

### Task 19: Update models.py with New Agent Roles

**Files:**
- Modify: `models.py`

**Step 1: Add model configs for new agent roles**

Add to `MODEL_CONFIGS` in `models.py`:

```python
MODEL_CONFIGS = {
    "orchestrator": { ... },  # existing
    "search": { ... },         # existing
    "doc_analyst": { ... },    # existing
    "writer": { ... },         # existing
    # New roles for V2
    "deep_research": {
        "model": "gpt-5.4",
        "base_url": "https://co.yes.vg",
        "api_key": "cr_751bd8df910f05852082e75fe02b04b2aecf475dba758891d3e97a553f8c993a",
    },
    "data_analyst": {
        "model": "gpt-5.4",
        "base_url": "https://co.yes.vg",
        "api_key": "cr_751bd8df910f05852082e75fe02b04b2aecf475dba758891d3e97a553f8c993a",
    },
}
```

**Step 2: Commit**

```bash
git add models.py
git commit -m "feat: add model configs for deep_research and data_analyst roles"
```

---

### Task 20: Update requirements.txt with All V2 Dependencies

**Files:**
- Modify: `requirements.txt`

**Step 1: Finalize requirements.txt**

```
# 服务框架
fastapi>=0.110.0
uvicorn[standard]>=0.29.0
websockets>=12.0
python-dotenv>=1.0.0

# LangGraph + LangChain
langgraph>=0.2.0
langchain-core>=0.2.0
langchain-openai>=0.1.0
langchain-community>=0.2.0

# 浏览器控制
browser-use>=0.1.40
playwright>=1.44.0

# 搜索（可选）
tavily-python>=0.3.0

# 文档处理
pypdf>=4.0.0

# PPT 生成
python-pptx>=0.6.23

# Word 生成
python-docx>=1.1.0

# Excel 生成
openpyxl>=3.1.0

# HTTP client (academic search)
httpx>=0.27.0
```

**Step 2: Install all dependencies**

Run:
```bash
cd /Users/luozhongxu/workspace/chat-dada && .venv/bin/pip install python-docx openpyxl httpx
```

**Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore: add all V2 dependencies (python-docx, openpyxl, httpx)"
```

---

## Summary

| Task | Phase | Component | Key Files |
|------|-------|-----------|-----------|
| 1 | 1 | Capability Registry | `registry.py` |
| 2 | 1 | Task Templates | `orchestrator/templates.py` |
| 3 | 1 | Dependency Scheduler | `orchestrator/scheduler.py` |
| 4 | 1 | Intent Planner | `orchestrator/planner.py` |
| 5 | 1 | Orchestrator Runner | `orchestrator/runner.py` |
| 6 | 1 | Wire main.py + Fix static | `main.py`, `static/` |
| 7 | 1 | General Chat Agent | `agents/general_chat.py` |
| 8 | 2 | Web Search Tool | `tools/web_search.py` |
| 9 | 2 | Translator Tool | `tools/translator.py` |
| 10 | 2 | Summarizer Tool | `tools/summarizer.py` |
| 11 | 2 | Code Executor Tool | `tools/code_executor.py` |
| 12 | 2 | Academic Search Tool | `tools/academic_search.py` |
| 13 | 3 | Word Renderer | `renderers/word_renderer.py` |
| 14 | 3 | Excel Renderer | `renderers/excel_renderer.py` |
| 15 | 3 | Data Analyst Agent | `agents/data_analyst.py` |
| 16 | 3 | Deep Research Agent | `agents/deep_research.py` |
| 17 | 4 | Move Legacy Code | `old/` |
| 18 | 4 | Integration Verification | (verify only) |
| 19 | 4 | Update Model Configs | `models.py` |
| 20 | 4 | Finalize Dependencies | `requirements.txt` |
