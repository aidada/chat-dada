---
title: OfficeCLI Verb-Discriminated Schema Redesign
date: 2026-04-19
status: proposed
owners:
  - chat-dada
scope:
  - Office domain
  - OfficeCLI tool schema
  - Tool compatibility layer
---

# Summary

This spec redesigns the `officecli` and `officecli_batch` tool input layer so that command validity is modeled structurally instead of being enforced primarily at runtime.

The redesign preserves these compatibility constraints:

- Keep the public tool names `officecli` and `officecli_batch`.
- Keep internal Office workflow and external callers on the same underlying execution path.
- Keep existing compatibility aliases such as `command` / `operation` -> `verb`.
- Preserve existing default-file behaviors that are already part of the runtime contract.

The key change is to replace the current flat `OfficeCliCommandInput` model with a canonical verb-discriminated command union that encodes verb-specific requirements before execution.

# Problem

The current tool model exposes a single flat schema where only `verb` is required and most other fields are nullable. This allows semantically invalid calls such as:

```json
{"verb": "view", "file": null, "mode": null}
```

to remain schema-valid at the model/tool boundary.

Today, correctness depends on later runtime checks in `agent/tools/officecli.py`. That creates three problems:

1. Invalid combinations are cheap for the model to emit.
2. The tool boundary does not teach the model real per-verb requirements.
3. Runtime rejection happens late enough to contribute to repeated-fatal-call loops.

# Goals

- Make OfficeCLI command validity structurally explicit at the tool-model layer.
- Reject invalid verb/field combinations before gateway or local execution.
- Preserve the existing two public tool names.
- Preserve internal/external convergence on the same underlying execution implementation.
- Keep backward compatibility for legacy field aliases and accepted default-file semantics.

# Non-Goals

- No redesign of the underlying OfficeCLI execution backend.
- No rename or split of the public tools into `officecli_view`, `officecli_add`, etc.
- No broad prompt redesign in this phase.
- No changes to non-Office domains.

# Proposed Design

## 1. Canonical command union

Introduce a canonical `OfficeCliCommand` type as a `verb`-discriminated union with one model per verb family.

Representative examples:

- `CreateCommand`
- `OpenLikeCommand` for `open|close|validate|watch|unwatch`
- `ViewCommand`
- `GetCommand`
- `QueryCommand`
- `SetCommand`
- `AddCommand`
- `RemoveCommand`
- `HelpCommand`

Each canonical model encodes the real semantic contract for that verb.

Examples:

- `ViewCommand` requires `mode`; `file` may remain logically derivable only if compatibility normalization can supply it.
- `HelpCommand` requires `format`.
- `GetCommand` / `SetCommand` / `RemoveCommand` require `path`.
- `QueryCommand` requires `selector`.
- `AddCommand` requires `parent` and `type`.

The canonical union becomes the source of truth for command validity.

## 2. Compatibility normalization before strict parsing

Public tool entrypoints must not parse raw caller input directly into the canonical union.

Instead, add a compatibility normalization step with this order:

1. Normalize legacy aliases:
   - `command` -> `verb`
   - `operation` -> `verb`
   - `properties` -> `props`
2. Apply existing runtime-derived defaults that are part of current behavior:
   - default create file
   - default current file where already supported by the runtime contract
3. Produce a normalized dict
4. Parse the normalized dict into the canonical `OfficeCliCommand`

This preserves old accepted shapes while ensuring the post-normalization object is structurally valid.

## 3. Keep public tool names unchanged

`officecli` remains the single-command entrypoint.

`officecli_batch` remains the batched entrypoint.

Internally:

- `officecli` normalizes raw input, parses into the canonical union, then passes a canonical dict to the shared execution path.
- `officecli_batch` performs the same normalization and strict parsing per command item, then executes the resulting canonical command list through the existing batch path.

No new public tool names are introduced.

## 4. Shared execution path remains intact

The canonical command layer sits above the existing executor in `agent/tools/officecli.py`.

The execution path remains:

- normalize input
- parse canonical command
- pass canonical dict to `execute_officecli_spec(...)`
- batch path still uses `execute_officecli_specs(...)`
- desktop/server routing logic remains unchanged

Runtime validation in `agent/tools/officecli.py` stays in place as defense-in-depth, but is no longer the primary expression of the contract.

## 5. Schema export expectations

The model-facing schema exported for `officecli` and `officecli_batch` should reflect verb-specific structure as closely as the provider path supports.

Primary target:

- the exported tool schema shows verb-discriminated structure instead of a single flat object with nullable fields.

Fallback if provider compatibility is weaker than expected:

- keep the same canonical internal union
- preserve compatibility normalization
- hand-control the exported schema while keeping the same two public tool names

This fallback is acceptable only if automatic export proves unreliable in the current MiniMax-compatible tool path.

# Data Flow

## Single command

1. model emits `officecli(...)`
2. compatibility normalization rewrites aliases and injects allowed defaults
3. normalized dict parses into canonical `OfficeCliCommand`
4. canonical command is dumped into a stable dict form
5. existing `execute_officecli_spec(...)` runs
6. existing gateway/local classification and result handling run unchanged

## Batch command

1. model emits `officecli_batch(commands=[...])`
2. each item is normalized independently
3. each normalized item parses into canonical `OfficeCliCommand`
4. canonical commands are dumped into stable dicts
5. existing `execute_officecli_specs(...)` runs

# Error Handling

- If alias normalization fails to produce a usable `verb`, return the existing fatal tool error shape.
- If compatibility normalization cannot supply required semantic fields for a specific verb, fail before execution.
- Parsing failures must be converted into clear tool-level fatal errors that identify the invalid command shape.
- Repeated-fatal-command blocking remains unchanged, but should trigger less often because invalid calls are rejected earlier and more deterministically.

# Testing

Add or update focused tests for:

- canonical parsing success for valid per-verb commands
- alias normalization compatibility (`command`, `operation`, `properties`)
- default-file compatibility that should remain supported
- invalid `view` command rejection before execution
- `officecli_batch` mixed-command parsing through the same canonical path
- exported tool schema expectations for `officecli` and `officecli_batch`

Regression target:

- a `view` command with missing `mode` must not reach gateway/local execution
- batch and single-command entrypoints must continue to converge on the same execution layer

# Rollout

Implement in one narrow slice:

1. add canonical command models
2. add compatibility normalization helpers
3. migrate `officecli` and `officecli_batch` wrappers to canonical parsing
4. keep existing executor behavior unchanged
5. update tests

This keeps the redesign localized to the Office tool boundary and avoids unrelated workflow churn.
