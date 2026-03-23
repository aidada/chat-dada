# Dynamic Workflow Orchestrator Design

## Background

The current system has 4 domain agents (research, patent, zero_report, ppt), each building its own LangGraph `StateGraph` from scratch. This leads to:

1. **Duplicated boilerplate**: evidence collection, citation mapping, artifact persistence, `_safe_emit`, deepagents integration repeated per agent
2. **Fixed execution paths**: each domain agent hardcodes its strategy (parallel OR sequential OR deepagents), with no runtime adaptability
3. **No composable strategies**: a research task can't dynamically switch from "decompose into subtasks" → "parallel execution" → "iterative refinement" based on intermediate results

## Goal

Build a **Dynamic Workflow Orchestrator** — an opt-in layer that:

1. Reduces boilerplate: domain agents declare a `DomainSpec`, the orchestrator handles execution
2. Enables runtime composition: strategies are selected dynamically based on task state (goal, progress, confidence, coverage, cost)
3. Uses a hybrid decision model: rule-based for common paths (~80%), LLM fallback for ambiguous states (~20%)

## Core Principle

The system uses **mixed strategies**, not a single pattern. Execution follows a control loop:

```
ANALYZE → SELECT STRATEGY → EXECUTE → EVALUATE → (loop or done)
```

Strategy order is NOT predefined — it is decided dynamically by state + evaluation.

## Architecture

### Layer Positioning

```
task_platform/root_graph.py          (routing, streaming, interrupts)
    └── Domain agent node
            └── DomainOrchestrator   (NEW: strategy selection + execution loop)
                    └── deepagents   (agent harness: planning, subagents, context)
                        └── tools    (domain-specific tools)
```

- **Root Graph**: decides WHICH domain to route to (existing)
- **Orchestrator**: decides HOW to execute within a domain (new)
- **DeepAgents**: provides the agent execution harness (existing library)
- **Domain Logic**: prompts, tools, schemas, reviewers (existing)

### New Directory Structure

```
workflows/
    __init__.py
    spec.py                  # DomainSpec — single interface for domain agents
    orchestrator.py          # build_orchestrated_graph() + strategy nodes + evaluate
    strategy_selector.py     # Hybrid rule+LLM strategy selection
```

## Two Core Abstractions

### 1. DomainSpec — what the domain provides

```python
@dataclass
class SubagentConfig:
    name: str
    description: str
    system_prompt: str
    tools: list[Any] = field(default_factory=list)

@dataclass
class DomainSpec:
    name: str                    # "research", "patent", etc.
    model_role: str              # key in MODEL_CONFIGS
    system_prompt: str           # main agent prompt
    tools: list[Any]
    subagents: list[SubagentConfig]
    evaluator: ReviewGate
    report_profile: str = ""
    strategy_hints: list[str] = field(default_factory=list)
    max_steps: int = 10
    max_cost: float = 5.0       # USD budget cap
```

### 2. build_orchestrated_graph(spec) — how the orchestrator uses it

Builds a LangGraph `StateGraph` with these nodes:

```
START → analyze → select_strategy → exec_{strategy} → evaluate → (analyze or END)
```

## Available Strategies (Composable)

| Strategy | What it does | When selected | DeepAgents usage |
|---|---|---|---|
| **Sequential** | Linear execution, one deepagents instance | Simple goal, single subtask, or default | Single `create_deep_agent()` with full tools+subagents |
| **Parallel** | Fan-out concurrent deepagents instances | 2+ independent pending subtasks | One `create_deep_agent()` per subtask + synthesizer |
| **Iterative** | Refine based on evaluation feedback | Last evaluation failed (confidence < 0.6) | `create_deep_agent()` with refinement prompt |
| **Planning** | Decompose goal into subtask coverage map | Complex goal without existing plan | `create_deep_agent()` for plan generation |

These are NOT exclusive — they compose naturally through state:
- Planning populates `coverage` → next step selects Parallel
- Parallel produces output → Evaluate finds issues → next step selects Iterative

## OrchestratorState

```python
class OrchestratorState(TypedDict, total=False):
    # Input
    goal: str
    task_id: str
    report_profile: str

    # Strategy control
    selected_strategy: str
    step_history: Annotated[list[dict], operator.add]

    # Progress signals (drive strategy selection)
    progress: float           # 0.0 – 1.0
    confidence: float         # 0.0 – 1.0
    coverage: dict[str, bool] # subtask_id → completed
    cost: float

    # Results
    intermediate_results: Annotated[list[dict], operator.add]
    evaluations: Annotated[list[dict], operator.add]
    final_result: str
```

## Strategy Selector — Hybrid Rule + LLM

### Layer 1: Rule-based (zero latency, zero cost)

Rules are pure functions evaluated in priority order. First match above confidence 0.75 wins.

| Priority | Rule | Condition | Strategy | Confidence |
|---|---|---|---|---|
| 1 | needs_refinement | Last eval failed, confidence < 0.6 | iterative | 0.95 |
| 2 | pending_parallel_subtasks | 2+ pending subtasks in coverage | parallel | 0.92 |
| 3 | complex_goal_no_plan | Complex goal + no coverage map + no prior planning step | planning | 0.88 |
| 4 | single_pending_or_simple | 1 subtask or short goal | sequential | 0.85 |
| 5 | domain_hints | spec.strategy_hints on first step only | (from hints) | 0.75 |

Complexity heuristics for rule 3: multi-step hints ("simultaneously", "also", "first...then"), goal length > 80 chars.

### Layer 2: LLM fallback (for ambiguous states)

When no rule fires above threshold, an LLM evaluates the full state and selects a strategy using structured output:

```python
class LLMStrategyDecision(BaseModel):
    strategy: str    # "sequential" | "parallel" | "iterative" | "planning"
    reasoning: str   # one-sentence justification
```

LLM decisions get a base confidence of 0.70 (lower than rule-based). On LLM failure, defaults to `sequential` with confidence 0.50.

### Tracing

Every selection decision is appended to `step_history`:

```json
{
    "strategy": "parallel",
    "confidence": 0.92,
    "reasoning": "3 independent subtasks pending",
    "source": "rule"
}
```

This enables full auditability of why each strategy was chosen.

## Evaluate Node

The evaluate node runs after every strategy execution:

1. Runs `spec.evaluator.evaluate()` (the domain's ReviewGate)
2. If passed: sets `final_result`, confidence → 0.9
3. If not passed: sets confidence → 0.4, issues are fed to next iterative step
4. Planning strategy outputs are auto-passed (they produce plans, not final output)

## Control Flow (should_continue)

The loop terminates when ANY of:
- `final_result` is set (evaluation passed)
- `cost >= max_cost` (budget limit)
- `len(step_history) >= max_steps` (step limit)

## Domain Agent Integration

### Opt-in pattern

A domain agent opts in by:

1. Creating a `DomainSpec` (declares what it knows)
2. Calling `build_orchestrated_graph(spec)` (gets a compiled LangGraph)
3. Exposing the same `run_X_domain(input_data)` interface

### Example: Research domain

```python
# domain_agents/research/orchestrated.py

RESEARCH_SPEC = DomainSpec(
    name="research",
    model_role="research_domain",
    system_prompt=RESEARCH_SYSTEM_PROMPT,
    tools=get_research_tools(),
    subagents=[
        SubagentConfig(name="web_researcher", ...),
        SubagentConfig(name="evidence_synthesizer", ...),
    ],
    evaluator=ResearchReviewGate(),
    strategy_hints=["planning"],
    max_steps=8,
    max_cost=3.0,
)

_graph = build_orchestrated_graph(RESEARCH_SPEC)

async def run_research_domain(input_data: dict) -> OrchestratedDomainResult:
    result = await _graph.ainvoke({
        "goal": input_data["query"],
        "task_id": input_data.get("task_id", ""),
        ...
    })
    return OrchestratedDomainResult(
        status="ok",
        result=result.get("final_result", ""),
        strategy_trace=result.get("step_history", []),
        ...
    )
```

### Root graph wiring (1 line change)

```python
# task_platform/root_graph.py
# Only change the node for the domain that opts in:
graph.add_node("run_research", _run_orchestrated_research_node)

# Other domains keep working as-is:
graph.add_node("run_patent", make_run_registered_domain("patent", ...))
graph.add_node("run_zero_report", make_run_registered_domain("zero_report", ...))
```

## Example Execution Trace

Research task: "compare transformer vs. SSM architectures in recent NLP papers"

```
Step 1: analyze (progress=0%, confidence=0%)
        → select_strategy: rule=complex_goal_no_plan → PLANNING (0.88)
        → exec_planning: deepagents decomposes into 3 subtasks
          [sub_1: "transformer architecture evolution", sub_2: "SSM/Mamba developments", sub_3: "comparative analysis"]
        → evaluate: plan generated, auto-pass

Step 2: analyze (progress=0%, confidence=70%)
        → select_strategy: rule=pending_parallel_subtasks (3 pending) → PARALLEL (0.92)
        → exec_parallel: 3 deepagents instances run concurrently → synthesize
        → evaluate: ReviewGate says confidence=0.5, missing citation depth

Step 3: analyze (progress=100%, confidence=40%)
        → select_strategy: rule=needs_refinement (confidence < 0.6) → ITERATIVE (0.95)
        → exec_iterative: deepagents refines with feedback from evaluation
        → evaluate: ReviewGate passes, confidence=0.9 → final_result set → DONE
```

3 different strategies composed dynamically across 3 steps.

## What Changes vs. What Doesn't

| Component | Changes? | Details |
|---|---|---|
| `workflows/` | **New** | `spec.py`, `orchestrator.py`, `strategy_selector.py` (~400 lines total) |
| `domain_agents/research/orchestrated.py` | **New** | ~50 lines, declares RESEARCH_SPEC |
| `domain_agents/research/agent.py` | **Unchanged** | Old path still works as fallback |
| `domain_agents/patent/agent.py` | **Unchanged** | Opts in later when ready |
| `domain_agents/zero_report/agent.py` | **Unchanged** | Opts in later when ready |
| `task_platform/root_graph.py` | **1 node swap** | Swap `run_research` node |
| `capabilities/review_gates.py` | **Unchanged** | Reused by evaluate node |
| `core/models.py` | **Unchanged** | Reused via `build_chat_model()` |

## Anti-Patterns to Avoid

- Do NOT follow a rigid pipeline — strategy order is state-driven
- Do NOT skip evaluation — every execution step must be evaluated
- Do NOT overuse parallelization blindly — only when subtasks are independent
- Do NOT assume first result is final — iterative refinement is expected
- Do NOT let the orchestrator become a god object — it's a graph definition, not a runtime singleton

## Implementation Order

1. `workflows/spec.py` — DomainSpec + SubagentConfig
2. `workflows/strategy_selector.py` — hybrid rule+LLM selector
3. `workflows/orchestrator.py` — graph builder + strategy nodes + evaluate
4. `domain_agents/research/orchestrated.py` — first domain integration
5. Wire into `task_platform/root_graph.py` — swap research node
6. Test with research domain, measure latency/quality/cost
7. Migrate patent and zero_report if results are positive
