---
title: Office/PPT Staged Execution Implementation Plan
date: 2026-04-16
status: active
depends_on:
  - docs/superpowers/specs/2026-04-16-office-ppt-staged-execution-design.md
owners:
  - chat-dada
---

# Office/PPT Staged Execution Implementation Plan

## Objective

Implement the staged Office/PPT execution redesign with these priorities:

1. Success rate first for medium and large decks, especially `6-12` page PPT tasks.
2. Quality second, including structured planning, visuals, transitions, speaker notes, and bounded QA repair.
3. Backend-visible cost accounting at task, stage, and call granularity.

This plan intentionally avoids a full job-system rewrite. The work is phased so that each phase is independently testable and deployable.

## Working Principles

- Preserve the top-level route: `Coordinator -> do_office -> office workflow -> OfficeCLI`
- Do not mix large architectural rewrites with bug-fix cleanup unless they directly serve this redesign
- Prefer explicit stage state over prompt-only behavior
- Prefer code-level convergence rules over “model should stop” instructions
- Make cost logging project-level and reusable, but start implementation with Office/PPT
- Treat the staged executor as **Office Core**, with PPT as the first strategy implementation rather than the final architecture

## Layering Strategy

The target architecture is:

- `Office Core`
  - stage machine
  - shared state
  - finalize
  - cost logging
  - error handling
- `Format Strategy`
  - `PptStrategy`
  - `DocxStrategy`
  - `XlsxStrategy`

The current migration remains PPT-first, but implementation choices should avoid baking slide-only semantics permanently into the Office Core layer.

## Implementation Phases

### Phase 1: Observability And Convergence

Goal: make Office/PPT failures diagnosable and stop known bad loops.

Scope:

- Formalize error categories:
  - `user_input_blocked`
  - `tool_contract_error`
  - `document_quality_failure`
  - `transport_runtime_failure`
- Improve structured OfficeCLI argument validation before execution
- Block repeated fatal commands at code level
- Improve termination messages to include stage, last successful mutation, last failed call, and partial-completion indicators
- Introduce task/stage/call cost logging schema and basic emitters

Primary files:

- `agent/tools/officecli.py`
- `agent/workflows/office/workflow.py`
- `agent/workflows/office/orchestrated.py`
- `agent/runtime/task_execution.py`
- relevant event/session persistence modules

Exit criteria:

- malformed OfficeCLI calls do not repeat indefinitely
- validation failures are correctly classified
- logs expose enough data to explain why a task stopped
- cost records exist for Office/PPT runs, even if some numbers are still estimated

### Phase 2: Staged Office/PPT Executor

Goal: replace the free-running inner loop with explicit stages and batch-oriented deck generation.

Scope:

- Introduce stage flow:
  - `planning`
  - `build`
  - `qa_fix`
  - `finalize`
- Add `GoalNormalizer`
- Add `DeckPlanner`
- Add `SectionBuilder`
- Replace fixed build recursion with dynamic build budgets
- Batch deck writing by section or `2-3` slides per batch

Primary files:

- `agent/workflows/office/workflow.py`
- `agent/workflows/office/orchestrated.py`
- new helper modules such as:
  - `agent/workflows/office/planner.py`
  - `agent/workflows/office/builder.py`
  - `agent/workflows/office/qa.py`

Exit criteria:

- long decks no longer fail merely because fixed `40`-step recursion was too small
- Office/PPT generation can report completed sections/batches when interrupted
- the system can identify whether a failure occurred in planning, build, QA, or finalize
- the resulting stage skeleton is extractable into Office Core without major behavior changes

### Phase 3: Quality Reinforcement

Goal: make output quality measurable and enforceable.

Scope:

- Require planner-produced `deck_plan` with:
  - `slide_number`
  - `role`
  - `takeaway`
  - `layout_type`
  - `visual_requirements`
  - `transition_required`
  - `notes_required`
- Introduce formal `QualityGate`
- Bound repair rounds in `qa_fix`
- Strengthen filename quality and semantic naming
- Expand quality checks for:
  - transitions
  - visual density
  - layout variety
  - speaker notes

Primary files:

- `agent/workflows/office/workflow.py`
- `agent/workflows/office/qa.py`
- `agent/coordinator/prompts.py`
- filename inference and planner output helpers

Exit criteria:

- deck quality checks are enforced in code, not just prompt text
- filenames are stable and aligned with goal semantics
- repair behavior is bounded and explainable

## Workstreams

### Workstream A: Execution Core

Deliverables:

- stage state machine
- dynamic build budgets
- batch-oriented builder
- finalize-only end state

Dependencies:

- minimal error classification from Phase 1

### Workstream B: Error Handling And Recovery

Deliverables:

- typed failure categories
- convergence guardrails
- bounded QA repair
- improved terminal diagnostics

Dependencies:

- none; can begin in parallel with Workstream A

### Workstream C: Cost And Observability

Deliverables:

- task-level cost record
- stage-level cost ledger
- model/tool call detail rows
- backend log integration

Dependencies:

- should begin in Phase 1 so later phases inherit instrumentation

### Workstream D: Input And Naming

Deliverables:

- normalized Office request profile
- better `file_hint` quality handling
- planner-aware filename generation
- stronger semantic prompt guidance

Dependencies:

- interacts with planning stage, but some filename improvements can ship earlier

## Milestones

### M1: Convergence Baseline

- repeated invalid OfficeCLI calls no longer loop indefinitely
- validation errors are correctly surfaced
- task cost summary is present in backend logs
- known bad traces become explainable

### M2: Stage Skeleton

- stage state exists in workflow state
- planning/build/finalize flow runs end-to-end
- dynamic build budget replaces fixed inner limit for create-heavy deck tasks

### M3: Batch Build

- planner emits section or batch plan
- builder executes `2-3` slides per batch
- medium and long decks complete with higher success rate

### M4: Formal QA/Fix

- `QualityGate` decides pass / fixable / hard-fail
- fix rounds are bounded
- partial completion and final status are clearly reported

### M5: Quality And Cost Maturity

- stage-level cost and quality metrics are stable
- long-deck regression traces are replayed successfully
- rollout decision can be based on measured improvements

## Detailed Task List

### Phase 1 Tasks

1. Add shared error-kind mapping helpers for Office/PPT execution.
2. Add structured tool-contract validation before OfficeCLI execution.
3. Add repeated-fatal-command block keyed by task scope.
4. Ensure validation outputs with discovered errors are marked failed.
5. Improve final error messages in orchestrated results with partial progress metadata.
6. Introduce backend cost-record schema for task/stage/call accounting.
7. Emit cost records for Office/PPT model calls and OfficeCLI calls.
8. Add tests for repeated fatal command blocking, validation classification, and cost aggregation helpers.

### Phase 2 Tasks

1. Extend Office workflow state with:
   - `task_profile`
   - `deck_plan`
   - `execution_state`
   - `qa_state`
   - `cost_state`
2. Implement `GoalNormalizer`.
3. Implement `DeckPlanner`.
4. Define planner output schema and validator.
5. Implement dynamic build budget calculation.
6. Implement `SectionBuilder` with batch execution.
7. Refactor workflow graph to use explicit stages.
8. Add partial-progress reporting for interrupted builds.
9. Add tests for staged transitions and dynamic budget behavior.

### Phase 3 Tasks

1. Implement `QualityGate`.
2. Standardize required QA calls and outputs.
3. Add bounded `qa_fix` loop with explicit stop criteria.
4. Add semantic filename refinement tied to planner output.
5. Add quality metrics for transitions, notes, layout variety, and visual density.
6. Add regression tests for long-deck quality outcomes and QA repair behavior.

## File Touch Map

### Existing files likely to change

- `agent/coordinator/prompts.py`
- `agent/coordinator/agent.py`
- `agent/workflows/office/workflow.py`
- `agent/workflows/office/orchestrated.py`
- `agent/tools/officecli.py`
- `agent/runtime/task_execution.py`
- relevant session/event logging modules

### New files expected

- `agent/workflows/office/planner.py`
- `agent/workflows/office/builder.py`
- `agent/workflows/office/qa.py`
- `agent/workflows/office/core/state.py`
- `agent/workflows/office/core/routing.py`
- `agent/workflows/office/core/finalize.py`
- optional cost helper module if current runtime/task execution abstractions are too coupled

## Migration Sequence

1. Stabilize the current staged Office workflow in place.
2. Extract shared state/routing/finalize/cost helpers into `office/core/`.
3. Move PPT-specific planning and quality logic into `office/strategies/ppt.py`.
4. Reduce `agent/workflows/ppt/` to a compatibility wrapper over the Office strategy.
5. Only then add `docx` and `xlsx` strategies.

This sequence ensures we do not combine:

- behavior change,
- major file movement,
- and cross-format expansion

in the same implementation slice.

## Testing Plan

### Unit tests

- filename generation and planner-friendly naming
- dynamic build budget calculation
- error classification
- validate-result parsing
- cost aggregation helpers

### Workflow tests

- normal stage progression
- build budget exhaustion
- fixable QA path
- hard-fail QA path
- finalize-only terminal behavior

### LangSmith replay regression

Replay these known patterns:

- misrouting/create-loop task family
- malformed `view` QA loop task family
- long-deck progressive-but-unfinished task family

Success criteria:

- no regression to repeated malformed tool loops
- improved explainability for stops
- higher long-deck completion rate

### Manual acceptance

Run representative tasks:

- `3-5` page deck
- `6-8` page deck
- `10-12` page deck with visuals, transitions, and notes requirements

Track:

- success rate
- page completion rate
- QA pass rate
- elapsed time
- task/stage cost

## Rollout Sequence

1. Ship Phase 1 behind the current Office/PPT entry path.
2. Enable Phase 2 staged flow for PPT create tasks first.
3. Extend the staged flow to broader Office create/transform tasks.
4. Enable Phase 3 quality gate after stage instrumentation is trusted.

## Risk Management

### Risk: planner output too verbose or unstable

Mitigation:

- enforce schema and size bounds on `deck_plan`
- keep slide specs concise and actionable

### Risk: builder becomes too rigid

Mitigation:

- allow bounded batch-local adjustment
- do not allow whole-deck replanning during build

### Risk: cost logging spreads inconsistent formulas

Mitigation:

- define one estimated-cost model and centralize it
- reuse the same record schema across domains

### Risk: phase rollout mixes unfinished architecture with production paths

Mitigation:

- keep each phase deployable on its own
- gate stage selection by task type initially

## First Execution Slice

The first code-execution slice after this plan lands should focus on the highest leverage items:

1. dynamic Office/PPT build budget based on requested slide count and quality profile
2. stronger batch-oriented planning hints
3. backend cost logging skeleton for task/stage/call accounting

This slice gives immediate value to long-deck generation without waiting for the entire staged executor to be complete.
