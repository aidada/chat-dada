# Office Reference-Driven Strategy Design

Date: 2026-04-17
Status: Draft for review
Scope: Office domain staged execution with high-fidelity reference-case support for `pptx`, `docx`, and `xlsx`

## Summary

This spec extends the staged `Office Core + Format Strategy` architecture so Office tasks can use reference files as first-class inputs. The system should support:

- goal-first generation and editing for `pptx`, `docx`, and `xlsx`
- high-fidelity reference alignment when users provide exemplar files
- consistent staged execution across formats:
  - `planning`
  - `build`
  - `qa_fix`
  - `finalize`

The key design decision is to add a shared `Reference Understanding Layer` between `Office Core` and each format strategy, instead of implementing reference handling separately inside `PptStrategy`, `DocxStrategy`, and `XlsxStrategy`.

## Problem

The current Office architecture already supports:

- shared staged execution in the Office workflow
- format-specific strategies
- PPT-first planning, build batching, and quality gates
- runtime cost logging, failure diagnostics, and partial progress reporting

However, reference-case handling is not yet a first-class capability. The missing piece is a shared mechanism that can:

- read reference Office files using OfficeCLI
- extract reusable structure and style constraints
- keep user goal requirements above reference constraints
- feed those constraints into format-specific planners and builders

Without this layer, each format strategy would need to reinvent:

- reference parsing
- constraint merging
- conflict resolution
- fidelity heuristics

This would lead to duplicated logic and inconsistent behavior across `pptx`, `docx`, and `xlsx`.

## Product Goals

### Shared goals

- Treat reference files as structured constraints, not as loose inspiration.
- Preserve a strict priority rule:
  - `goal_constraints > reference_constraints`
- Support both `create` and `edit` flows for all three formats.
- Expose enough diagnostics for users and operators to understand:
  - what was copied from the reference
  - what was intentionally changed to satisfy the goal
  - where fidelity could not be preserved

### Format-specific goals

#### PPT

- Reproduce slide organization, layout rhythm, visual density, and style patterns from a reference deck.
- Preserve goal-first planning for content and page count.

#### DOCX

- Generate or edit documents that are tightly aligned with the user goal.
- Use reference files to match heading systems, section organization, tables, and writing style conventions.
- For users who provide a template/example, align formatting and structure as closely as possible.

#### XLSX

- Generate or edit workbooks whose sheet structure and regions strictly match the user specification.
- Use reference files to match workbook topology, table schemas, summary zones, formatting conventions, and chart layout.

## Non-Goals

This spec does not promise:

- perfect template cloning for every OOXML edge case on day one
- full parity of high-fidelity depth across all three formats in the first rollout
- arbitrary semantic data analysis or financial modeling beyond requested workbook structure

The design does assume OfficeCLI is powerful enough to support a high-fidelity strategy path, including fallback to lower-level operations when higher-level calls are insufficient.

## OfficeCLI Capability Assumption

This design is based on the current `iOfficeAI/OfficeCLI` capability envelope, not on an imaginary future tool.

The current tool already supports:

- `pptx`, `docx`, `xlsx`
- read, create, modify, and validate flows
- high-level inspection via `view`
- node-level manipulation via `get`, `query`, `set`, `add`, `remove`, `move`, `swap`
- lower-level escape hatches such as `raw`, `raw-set`, and OOXML-oriented operations

This is sufficient to begin reference-driven strategies now.

The practical implication is:

- reference fidelity should use a layered strategy:
  - `L1`: high-level inspect
  - `L2`: structured node operations
  - `L3`: raw fallback for high-fidelity gaps

## Architecture

### Layers

The target architecture becomes:

1. `Office Core`
2. `Reference Understanding Layer`
3. `Format Strategy Layer`

#### Office Core

Shared staged runtime for all Office tasks:

- `planning`
- `build`
- `qa_fix`
- `finalize`

Shared responsibilities:

- stage orchestration
- partial-progress reporting
- cost logging
- failure classification
- final result shaping
- artifact flush and finalize behavior

#### Reference Understanding Layer

Shared layer that converts reference files and existing files into reusable constraints.

Components:

- `ReferenceInspector`
- `ReferenceProfiler`
- `ConstraintResolver`

#### Format Strategy Layer

Consumes goal constraints and reference constraints to produce format-specific execution plans.

Strategies:

- `PptStrategy`
- `DocxStrategy`
- `XlsxStrategy`

## Shared Data Model

### Task profile

`task_profile` should include:

- `format`
- `operation`
- `target_filename`
- `source_files`
- `reference_files`
- `runtime_target`
- `quality_profile`

### Reference understanding outputs

The shared reference layer outputs:

- `goal_constraints`
- `reference_structure_constraints`
- `reference_style_constraints`
- `existing_document_profile`
- `conflict_resolution`

#### goal_constraints

Captures what the user actually wants to deliver:

- required content
- required sections / sheets / slides
- hard requirements
- output shape and completeness constraints

#### reference_structure_constraints

Captures how the reference file is organized:

- section order
- sheet topology
- slide sequence
- canonical component organization

#### reference_style_constraints

Captures how the reference file looks and feels:

- layout conventions
- table / chart conventions
- heading conventions
- formatting and style patterns
- theme and visual system patterns

#### existing_document_profile

Only used for `edit` flows. Describes the current file state, such as:

- current structure
- existing style system
- protected zones
- mutable zones
- dependencies that should not be broken

#### conflict_resolution

This is explicit and not inferred ad hoc:

- `goal > reference`
- if the reference conflicts with the user goal, preserve the goal and record a structured deviation reason

## Reference Understanding Layer

### ReferenceInspector

Reads the supplied reference file through OfficeCLI.

Primary read path:

- `view`
- `get`
- `query`

Fallback path:

- `raw`
- `raw-set` only for repair, not for read unless necessary

Per-format examples:

- `pptx`
  - outline, stats, annotated view
  - layout nodes, placeholder nodes, theme-related nodes
- `docx`
  - headings, paragraphs, tables, sections, style references
- `xlsx`
  - sheet list, table regions, header ranges, formula zones, summary zones, chart zones

### ReferenceProfiler

Transforms inspection output into reusable constraints.

It should not emit free-form prose only. It must emit structured summaries that strategies can consume deterministically.

Expected profiler outputs by format:

- `pptx`
  - slide roles
  - layout families
  - visual density profile
  - title style profile
  - notes / transition usage profile
- `docx`
  - heading hierarchy
  - section order profile
  - paragraph/table balance
  - style family profile
- `xlsx`
  - workbook topology
  - sheet roles
  - region map
  - schema and formula conventions
  - chart and summary placement profile

### ConstraintResolver

Combines:

- `goal_constraints`
- `reference_structure_constraints`
- `reference_style_constraints`
- `existing_document_profile`

Outputs merged constraints that downstream strategies consume.

It must also emit structured deviations when goal and reference conflict. Example:

- `style_deviation`
- `structure_deviation`
- `protected_zone_conflict`

These deviations feed diagnostics and QA summaries.

## Operation Split

`create` and `edit` are shared Office Core operations, but they must branch inside each strategy.

### Create

Inputs:

- `goal_constraints`
- `reference_structure_constraints`
- `reference_style_constraints`

Output:

- a new format-specific plan

Behavior:

- plan first
- create new file structure
- reconstruct reference-aligned structure and style around the user goal

### Edit

Inputs:

- `goal_constraints`
- `reference_*_constraints`
- `existing_document_profile`

Output:

- `edit_plan`

`edit_plan` must include:

- `target_units`
- `protected_units`
- `required_updates`
- `forbidden_changes`

This prevents edit mode from degenerating into accidental full rewrites.

## Format Strategies

### PptStrategy

Consumes shared constraints and produces:

- `deck_plan`
- `batch_plan`
- `quality_targets`

Focus of fidelity:

- slide sequence
- layout rhythm
- visual density
- title hierarchy
- notes / transitions
- theme and structure alignment

Execution unit:

- `slide batch`

### DocxStrategy

Primary first-release scenarios:

- reports
- proposals
- meeting notes
- explanatory documents

Consumes shared constraints and produces:

- `document_plan`
- `section_plan`
- `quality_targets`

Each section should include:

- `heading`
- `purpose`
- `key_points`
- `content_mode`
- `style_requirements`

Focus of fidelity:

- heading structure
- section order
- paragraph and table usage
- style system consistency
- document organization aligned to the reference

Execution unit:

- `section`

### XlsxStrategy

Primary first-release scenarios:

- ledgers
- statistics sheets
- budget sheets
- analysis workbooks

Consumes shared constraints and produces:

- `workbook_plan`
- `sheet_plan`
- `quality_targets`

Each sheet should include:

- `name`
- `purpose`
- `sheet_type`
- `columns`
- `table_regions`
- `formula_regions`
- `chart_regions`
- `validation_rules`

Focus of fidelity:

- workbook topology
- sheet role alignment
- header and schema correctness
- summary and chart placement
- formatting and region conventions

Execution unit:

- `sheet`
- optionally `sheet region` for granular edit workflows

## Planner Contracts

Each strategy should expose:

- `plan(task_profile, merged_constraints) -> plan`
- `validate_plan(plan) -> normalized_plan, issues`
- `build_input_sections(...) -> list[str]`
- `evaluate_quality_stats(...) -> issues`
- `advance_after_build(...) -> transition`

The `validate_plan` step is mandatory for all formats. It ensures:

- required fields exist
- units are normalized
- build batches or units are coherent
- fallback defaults are inserted when the planner under-specifies something

## Build Strategy

### Shared rule

Build should follow:

1. create or open target
2. create coarse structure
3. fill content and structure by unit
4. repair high-fidelity gaps with lower-level calls if required
5. run QA

### Fidelity ladder

To keep reference support realistic:

- first try high-level OfficeCLI operations
- if required fidelity is not reachable, drop to lower-level structured mutations
- only use raw-level operations for narrow gaps that are necessary for target fidelity

This keeps the system maintainable while still permitting high-fidelity reconstruction.

## Quality Gates

### Shared quality gate semantics

Every format should produce:

- `quality_report`
- `quality_report_summary`

Statuses:

- `passed`
- `fixable`
- `hard_fail`

### PPT quality checks

- required QA calls executed
- slide count and batch completion
- layout variety
- visual density
- notes coverage
- transition coverage
- reference-aligned structure and style

### DOCX quality checks

- heading hierarchy validity
- section coverage against goal
- paragraph/table structure completeness
- style consistency
- reference-aligned organization and formatting
- edit boundary safety

### XLSX quality checks

- sheet topology correctness
- schema and header correctness
- formula region integrity
- summary/chart region presence
- reference-aligned workbook layout
- edit boundary safety

## Diagnostics and Progress

The system should continue using:

- `cost_ledger`
- `quality_report_summary`
- `partial_progress`

Reference-driven execution should add:

- fidelity deviation summaries
- protected-zone violation summaries
- reference parsing coverage summaries

This is especially important for `edit` failures and partial completions.

## Rollout Plan

### Phase A

Build the shared `Reference Understanding Layer`.

Deliverables:

- `ReferenceInspector`
- `ReferenceProfiler`
- `ConstraintResolver`
- shared constraint schema
- PPT retrofit

Why PPT first:

- current Office architecture is already PPT-first
- OfficeCLI exposes strong PPT inspection and mutation capabilities
- fidelity is easier to observe and test

### Phase B

Add `XlsxStrategy` on top of the shared reference layer.

Why XLSX second:

- structural correctness is highly testable
- workbook topology and region fidelity are concrete
- OfficeCLI already exposes strong spreadsheet-oriented capabilities

### Phase C

Add `DocxStrategy` high-fidelity reinforcement.

Why DOCX last:

- it has the broadest gray area around style fidelity
- it is most likely to require raw-level fallback
- it benefits from a proven shared reference layer first

## Testing Strategy

### Unit tests

- reference constraint extraction
- constraint resolution precedence
- plan validation for all formats
- fidelity deviation reporting

### Workflow tests

- create with goal only
- create with goal + reference
- edit with existing file + goal
- edit with existing file + goal + reference
- protected-unit enforcement
- partial-progress behavior under bounded failure

### Format-specific regression suites

#### PPT

- reference layout fidelity
- notes / transition preservation
- high-fidelity batch execution

#### DOCX

- heading hierarchy and section alignment
- style-system alignment
- edit boundary safety

#### XLSX

- workbook topology
- schema fidelity
- formula region preservation
- chart/summary placement fidelity

## Risks

### Risk 1: Over-promising fidelity

Mitigation:

- use the L1/L2/L3 fidelity ladder
- record deviations explicitly
- gate rollout format by format

### Risk 2: Strategy duplication

Mitigation:

- centralize reference parsing and constraint resolution
- keep only format-specific planning and QA in strategies

### Risk 3: Edit-mode damage

Mitigation:

- require `existing_document_profile`
- require `edit_plan` with `protected_units`
- hard-fail when protected zones would be mutated

## Recommendation

Adopt:

- shared `Reference Understanding Layer`
- shared Office Core staged executor
- format strategies for `pptx`, `docx`, and `xlsx`

Roll out in this order:

1. shared layer + PPT retrofit
2. XLSX strategy
3. DOCX high-fidelity reinforcement

This gives the project a long-term-correct architecture while staying grounded in the actual capabilities of the current OfficeCLI backend.
