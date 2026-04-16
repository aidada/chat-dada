---
title: Office/PPT Staged Execution Redesign
date: 2026-04-16
status: proposed
owners:
  - chat-dada
scope:
  - Office domain
  - PPT generation
  - Task-level cost logging
---

# Office/PPT Staged Execution Redesign

## Summary

This spec redesigns the current Office/PPT generation flow from a single free-running inner agent loop into a staged execution pipeline optimized for two priorities:

1. Higher success rate for medium and large PPT tasks, especially 6-12 page decks.
2. Higher output quality, including layout variety, visuals, transitions, notes, and explicit QA gates.

The redesign also promotes error classification and cost accounting into first-class mechanisms at the project level, rather than leaving them as prompt-only conventions or ad hoc logging.

This design keeps the existing top-level entry path:

- `Coordinator -> do_office -> office workflow -> OfficeCLI`

but replaces the internal execution style with explicit stages, stage-scoped budgets, stage-scoped cost accounting, and stricter convergence behavior.

## Problem Statement

The current Office/PPT flow exhibits two distinct failure modes:

1. **True error loops**
   The inner agent repeats invalid or fatal OfficeCLI calls until `inner_recursion_limit` is reached. This occurred in traces where the agent repeatedly emitted malformed `view` calls.

2. **Legitimate progress that still runs out of steps**
   The agent successfully advances the deck but operates at too fine a granularity, causing long decks to exhaust the fixed inner recursion budget before completing.

The current design has additional structural weaknesses:

- The inner agent handles planning, writing, QA, and repair in one loop.
- Long-form PPT generation is too sensitive to the quality of a single iterative loop.
- QA behavior is partly encoded in prompt text rather than enforced by code.
- Cost information exists in fragments across the codebase but is not recorded as an authoritative task-level or stage-level ledger for Office/PPT tasks.
- Termination reasons are not sufficiently explanatory for operators or users.

## Goals

- Increase success rate for 6-12 page PPT generation tasks.
- Improve quality consistency for deck structure, visual density, transitions, speaker notes, and validation outcomes.
- Replace fixed inner-agent behavior with staged execution and stage-aware budgets.
- Make errors formally classified and actionable.
- Record task/stage/model/tool cost in backend logs for every Office/PPT run.
- Preserve the existing Coordinator and `do_office` integration shape so the redesign can be rolled out incrementally.

## Non-Goals

- No UI cost dashboard in this phase. Cost visibility lands first in backend logs and task detail surfaces.
- No complete job-system rewrite or durable multi-day resumable workflow in this phase.
- No replacement of OfficeCLI as the underlying document mutation engine.
- No broad redesign of non-Office domains except for shared cost/error mechanisms where explicitly stated.

## Recommended Approach

Adopt a **staged Office/PPT executor** with four formal phases:

1. `planning`
2. `build`
3. `qa_fix`
4. `finalize`

This is preferred over both:

- minimal patching of the current inner loop, which does not adequately solve large-deck completion, and
- a full background job architecture rewrite, which is too large for the current scope.

## Proposed Architecture

### Top-Level Flow

The external orchestration path remains:

- Coordinator decides `single_skill -> do_office`
- Office orchestrated entrypoint prepares task context
- Office workflow executes a staged internal pipeline
- OfficeCLI remains the execution backend for document mutation and validation

### Internal Stages

#### 1. Planning

The planning stage converts a normalized Office/PPT request into a structured `deck_plan`.

Responsibilities:

- interpret user goal
- infer requested slide count, content profile, and quality profile
- generate a suitable target filename
- produce section boundaries
- define slide-by-slide intent and layout requirements
- define batch boundaries for the build stage
- define QA expectations that must be met before finalize

This stage does not perform OfficeCLI mutations.

#### 2. Build

The build stage writes the PPT in batches rather than page-by-page freeform iterations.

Responsibilities:

- create the target file
- create the required slide skeleton
- fill content for `2-3` slides per batch
- attach transitions and notes according to the plan
- capture per-batch execution summaries

This stage should not redesign the whole deck on every round. It executes the planner’s contract.

#### 3. QA/Fix

The QA/Fix stage runs structured checks and performs bounded repairs.

Responsibilities:

- run `validate`
- run `view stats`
- run `view annotated`
- optionally run `view outline` or `view issues`
- classify findings into pass / fixable / hard-fail
- perform at most a bounded number of fix rounds

This stage is where output quality becomes a formal gate, not just a prompt expectation.

#### 4. Finalize

The finalize stage is reserved for:

- `close/flush`
- artifact registration
- final task summary
- cost aggregation
- terminal status output

No return to build is allowed after finalize begins.

## Component Design

### GoalNormalizer

Purpose: Normalize incoming Office/PPT requests before planning.

Inputs:

- goal text
- `format_hint`
- `operation_hint`
- `source_files`
- conversation context where relevant

Outputs:

- `task_profile`

Key fields:

- `format`
- `operation`
- `requested_slide_count`
- `quality_profile`
- `target_filename`
- `file_hint`
- `source_files`

### DeckPlanner

Purpose: Convert normalized input into a stable `deck_plan`.

Outputs:

- deck title
- section list
- slide list
- section-to-slide mapping
- batch plan
- QA expectations

Each slide spec should include:

- `slide_number`
- `role`
- `takeaway`
- `layout_type`
- `visual_requirements`
- `transition_required`
- `notes_required`

Each batch spec should include:

- `batch_index`
- `section_name`
- `slide_range`
- `target_operations`
- `qa_checkpoint`

### SectionBuilder

Purpose: Execute one batch from the deck plan.

Responsibilities:

- consume one batch spec
- translate the batch into OfficeCLI calls
- summarize success or failure
- record batch-level tool and cost metrics

The builder must not emit shell-based fallback in Office/PPT mode.

### QualityGate

Purpose: Turn OfficeCLI QA outputs into structured judgments.

Inputs:

- validate output
- stats output
- annotated output
- optional outline/issues output

Outputs:

- `pass`
- `fixable`
- `hard_fail`
- structured issue list

### TaskCostRecorder

Purpose: Persist authoritative cost and runtime accounting.

Scope:

- project-level mechanism, not Office-only
- Office/PPT is the first high-value adopter

Responsibilities:

- task-level totals
- stage-level ledger
- per-call detail rows

## Data Flow

1. `Coordinator` produces `single_skill + do_office` with semantic hints.
2. `GoalNormalizer` creates `task_profile`.
3. `DeckPlanner` creates `deck_plan`.
4. `SectionBuilder` executes build batches sequentially.
5. `QualityGate` evaluates the produced document.
6. If QA is `fixable`, bounded repair runs and QA is re-executed.
7. `Finalize` closes the file, records artifacts, and emits the final result.
8. `TaskCostRecorder` records totals and stage/call details throughout.

## State Model

The Office/PPT workflow state should be extended with explicit stage-aware fields.

### task_profile

Static request profile:

- `format`
- `operation`
- `requested_slide_count`
- `quality_profile`
- `target_filename`
- `file_hint`
- `source_files`

### deck_plan

Planner contract:

- `title`
- `sections`
- `slides`
- `batch_plan`
- `qa_expectations`

### execution_state

Mutable run status:

- `current_stage`
- `current_batch_index`
- `completed_batches`
- `failed_batches`
- `stage_retry_counts`
- `completed_slide_count`

### qa_state

QA memory:

- `last_validate`
- `last_stats`
- `last_annotated`
- `issues`
- `fix_round`

### cost_state

Authoritative metrics:

- `total_cost_usd`
- `model_cost_usd`
- `tool_cost_usd`
- `stage_costs`
- `tool_counts`
- `elapsed_ms`

## Budget And Convergence Rules

### Stage Budgets

Budgets are stage-specific rather than governed by a single flat recursion limit.

#### Planning

- small budget
- usually `1-2` model rounds

#### Build

Dynamic budget:

- base formula: `24 + requested_slide_count * 6`
- add `+8` if animations are explicitly required
- add `+6` if visual richness is explicitly required
- add `+4` if notes coverage is required

This budget is used as the main bound for multi-batch creation work.

#### QA/Fix

- fixed low budget
- maximum `2-3` repair rounds

#### Finalize

- no return to build
- close-only operations

### Convergence Rules

- Repeated identical fatal command: immediate stop.
- Repeated identical malformed tool contract error: immediate stop.
- Validation passed and QA expectations satisfied: no more tool calls allowed.
- Build budget exhausted: return partial-completion result with stage-specific diagnostics.
- Transport/runtime failure: stop the current stage immediately; do not continue deck mutation blindly.

## Error Classification

Errors become formal project-level categories:

### 1. user_input_blocked

Examples:

- missing source file for edit
- filename conflict that requires explicit confirmation

Handling:

- clarification interrupt
- not counted as task failure

### 2. tool_contract_error

Examples:

- `view` without `mode`
- `set` without `path`
- `add` without `type`

Handling:

- no repeated retry
- stage hard-fail
- emit exact invalid tool payload in diagnostics

### 3. document_quality_failure

Examples:

- validate fails
- stats do not meet visual/notes/layout thresholds
- annotated view shows text-only slides

Handling:

- enter `qa_fix`
- bounded repair only

### 4. transport_runtime_failure

Examples:

- desktop timeout
- permission unavailable
- sidecar failure
- close/flush failure

Handling:

- stop current stage
- do not continue deck mutation
- report environment-side failure clearly

## Filename Strategy

Filename generation should prioritize:

1. explicit user-provided filename
2. planner-proposed semantic filename
3. deterministic fallback from normalized intent

Requirements:

- kebab-case
- concise English noun phrase
- aligned to goal semantics
- no generic outputs such as `ai.pptx`, `deck.pptx`, `presentation.pptx`

Examples:

- `chat-dada-agent-intro.pptx`
- `ai-era-child-modern-education.pptx`
- `modern-ai-parenting-guide.pptx`

## Cost Logging Design

Cost visibility lands first in backend logs.

### Task-Level Record

Fields:

- `task_id`
- `domain`
- `status`
- `current_stage`
- `requested_pages`
- `completed_pages`
- `total_cost_usd`
- `model_cost_usd`
- `tool_cost_usd`
- `elapsed_ms`

### Stage-Level Record

Fields:

- `task_id`
- `stage`
- `start_at`
- `end_at`
- `elapsed_ms`
- `model_calls`
- `tool_calls`
- `cost_usd`
- `result_status`

### Call-Level Record

Fields:

- `task_id`
- `stage`
- `call_type`
- `name`
- `model_name` or `tool_name`
- `input_tokens`
- `output_tokens`
- `estimated_cost_usd`
- `execution_time_ms`
- `result_kind`
- `command` where applicable

### Accuracy Model

The system should initially use `estimated_cost_usd` with a consistent internal pricing model. Absolute billing accuracy is not required in phase 1; comparability and structural insight are the priority.

## Testing Strategy

### Unit Tests

Cover:

- filename generation
- deck plan structure validation
- stage budget calculation
- error classification
- cost aggregation

### Workflow Tests

Cover:

- normal `planning -> build -> qa_fix -> finalize`
- fixable QA path
- hard-fail QA path
- budget exhaustion path
- transport/runtime interruption path

### LangSmith Replay Regression

Replay known failure patterns:

- misrouting/create loops
- malformed QA loops
- long-deck progressive but unfinished runs

Success criteria:

- no regression to prior repeated-failure paths
- clearer termination causes
- cost logs produced

### Manual Acceptance

Run representative tasks:

- short deck: `3-5` pages
- medium deck: `6-8` pages
- long deck: `10-12` pages with visuals and transitions

Track:

- completion rate
- page completion rate
- QA pass rate
- notes/transition coverage
- total and per-stage cost visibility

## Rollout Plan

### Phase 1: Observability And Convergence

- formal error classification
- repeated-failure convergence guard
- task/stage/call cost logging
- improved terminal diagnostics

### Phase 2: Staged PPT Execution

- `GoalNormalizer`
- `DeckPlanner`
- `SectionBuilder`
- stage-aware state model
- dynamic build budgets

### Phase 3: Quality Reinforcement

- formal `QualityGate`
- stronger visual/layout/notes checks
- planner-side quality constraints
- future UI cost consumption

## Risks

### Risk: Planner over-specifies slides

Mitigation:

- keep planner output structured but concise
- use section-level batching rather than ultra-detailed low-level commands

### Risk: Build stage becomes too rigid

Mitigation:

- allow bounded batch-local adjustment
- do not allow whole-deck replanning inside build

### Risk: Cost logging becomes domain-specific

Mitigation:

- define the logging schema at the project level
- use Office/PPT as the first implementation target

## Decision

Proceed with a staged Office/PPT execution redesign centered on:

- explicit phases
- dynamic build budgets
- project-level error classification
- project-level cost logging
- batch-oriented PPT writing

This provides the best balance for the stated priorities: success rate and quality first, with backend-visible cost accounting and acceptable willingness to trade latency for better outcomes.
