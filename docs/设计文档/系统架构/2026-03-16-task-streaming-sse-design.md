# Task Streaming Refactor Design

## Background

The current application couples task execution to a single WebSocket connection:

- The client submits a task over `/ws`.
- The server executes the task inline within the WebSocket handler.
- Progress is pushed as `start`, `step`, `file`, `result`, `error`, and `monitoring` messages.
- Any browser refresh, `uvicorn --reload` restart, or transient disconnect causes the client to lose visibility into the running task.

This behavior is a poor match for the product interaction model. The UI is not a rich bidirectional real-time session. It is a one-shot task submission flow followed by server-originated progress updates.

## Recommended Architecture

Use `POST + SSE + persistent task state`.

### Transport choice

- `POST /tasks` creates a task and returns `task_id`.
- `GET /tasks/{task_id}/events` streams task events via Server-Sent Events.
- `GET /tasks/{task_id}` returns current task status and summary metadata.
- `POST /tasks/{task_id}/cancel` is reserved for future cancellation support.

WebSocket is not the recommended primary transport for this project because the current UX does not require high-frequency bidirectional messaging. SSE maps directly to the actual requirement: submit once, then receive ordered progress events.

### Core principle

Task execution must be independent from any single client connection.

The transport layer becomes a subscriber to task state, not the owner of task state.

## Data Model

Introduce a persistent `TaskRunStore` backed by SQLite.

### `task_runs`

Fields:

- `task_id` TEXT PRIMARY KEY
- `user_id` TEXT NOT NULL
- `status` TEXT NOT NULL
  - `queued`
  - `running`
  - `succeeded`
  - `failed`
  - `cancelled`
- `task_text` TEXT NOT NULL
- `thinking_level` TEXT NOT NULL
- `request_payload_json` TEXT NOT NULL
- `result_text` TEXT NULL
- `error_text` TEXT NULL
- `created_at` TEXT NOT NULL
- `started_at` TEXT NULL
- `finished_at` TEXT NULL
- `updated_at` TEXT NOT NULL

### `task_events`

Fields:

- `task_id` TEXT NOT NULL
- `seq` INTEGER NOT NULL
- `event_type` TEXT NOT NULL
  - `start`
  - `step`
  - `file`
  - `result`
  - `error`
  - `monitoring`
  - optional future types: `heartbeat`, `status`
- `payload_json` TEXT NOT NULL
- `created_at` TEXT NOT NULL

Primary key:

- `(task_id, seq)`

### Event contract

Every user-visible event must be stored before or at the same time it is emitted to a live client.

Each event payload should include:

- `task_id`
- `seq`
- `type`
- `content`
- optional structured fields per type

Example:

```json
{
  "task_id": "task_123",
  "seq": 4,
  "type": "step",
  "content": "📝 Generating storyline..."
}
```

## API Design

### `POST /tasks`

Purpose:

- Accept a new task submission.
- Persist the task row.
- Start background execution.
- Return task metadata immediately.

Request body:

```json
{
  "task": "hi",
  "user_id": "web-...",
  "thinking_level": "medium",
  "file_paths": []
}
```

Response:

```json
{
  "task_id": "task_123",
  "status": "queued"
}
```

Behavior:

- Validate input.
- Expand uploaded file paths into task text exactly as current WebSocket flow does.
- Create `task_runs` row with `queued`.
- Spawn background coroutine or task runner.
- Return `202 Accepted` semantics even if implemented as `200`.

### `GET /tasks/{task_id}`

Purpose:

- Retrieve current status after page reload or before opening the event stream.

Response:

```json
{
  "task_id": "task_123",
  "status": "running",
  "task": "hi",
  "result": null,
  "error": null,
  "updated_at": "2026-03-16T10:00:00Z"
}
```

### `GET /tasks/{task_id}/events`

Purpose:

- Stream ordered events using SSE.

SSE event shape:

```text
id: 4
event: step
data: {"task_id":"task_123","seq":4,"type":"step","content":"📝 Generating storyline..."}

```

Behavior:

- Read `Last-Event-ID` header when present.
- Query and replay all stored events with `seq > last_event_id`.
- Keep the connection open while the task is `queued` or `running`.
- Emit new events as they are appended.
- Close naturally after sending terminal event plus optional final monitoring event.

### `POST /tasks/{task_id}/cancel`

Purpose:

- Reserve a stable future interface for cancellation.

Current design requirement:

- Add the endpoint contract in the doc, but implementation can be deferred.

## Backend Flow

### Task creation

1. Client uploads files through existing `/upload`.
2. Client calls `POST /tasks`.
3. Server stores the task row and returns `task_id`.
4. Server launches background execution using the existing orchestrator pipeline.

### Event recording

Wrap the current `on_step` callback with a task event recorder:

1. Allocate the next `seq` for the task.
2. Persist the event to `task_events`.
3. Publish the event to any active in-process subscribers.

The orchestrator remains mostly unchanged. It still emits logical progress updates through a callback. The callback implementation changes from “send over websocket” to “append event + publish”.

### Terminal state handling

On success:

- persist `result_text`
- append `result`
- append `monitoring`
- set task status to `succeeded`

On failure:

- persist `error_text`
- append `error`
- append `monitoring`
- set task status to `failed`

## Frontend Flow

### New submission flow

1. Upload files through existing `/upload`.
2. Call `POST /tasks`.
3. Save `task_id` in memory and `sessionStorage`.
4. Clear the visible log only when a new `task_id` is created.
5. Open `EventSource` to `/tasks/{task_id}/events`.

### Reconnect flow

When the page reconnects or reloads:

1. Read `active_task_id` from `sessionStorage`.
2. Call `GET /tasks/{task_id}`.
3. If task status is terminal, restore the last rendered task from stored events or fetch replayed events once.
4. If task status is `queued` or `running`, reopen `EventSource`.
5. Browser automatically sends `Last-Event-ID`; server replays missing events.

### Client-side state

Persist in `sessionStorage`:

- `active_task_id`
- last rendered event id per task
- optional cached rendered event list for instant paint after reload

UI state rules:

- Do not mark the task failed on transient disconnect.
- Replace “任务中止” with “连接断开，正在重连；任务可能仍在继续”.
- Only mark failure when a terminal `error` event arrives or `GET /tasks/{task_id}` reports `failed`.

### Rendering rules

- Render events by `task_id + seq`.
- Ignore duplicates.
- Never call `clearLog()` during reconnect/replay for the same task.
- Append replayed historical events in-order before live streaming resumes.

## Implementation Plan

### Phase 1: Task state foundation

- Add SQLite-backed `TaskRunStore`.
- Add `task_runs` and `task_events` schema initialization.
- Add append/read methods for task events.
- Add in-process subscriber hooks for live event fan-out.

### Phase 2: HTTP task APIs

- Add `POST /tasks`.
- Add `GET /tasks/{task_id}`.
- Add `GET /tasks/{task_id}/events` as SSE endpoint.
- Keep existing `/upload` unchanged.

### Phase 3: Orchestrator integration

- Extract current WebSocket-bound execution logic into a task service.
- Replace direct socket writes with event appends.
- Ensure final `result/error/monitoring` are always persisted.

### Phase 4: Frontend migration

- Replace direct WebSocket task submission with `POST /tasks`.
- Replace WebSocket progress stream with `EventSource`.
- Add reconnect and replay behavior keyed by `task_id`.
- Update disconnect UI messaging.

### Phase 5: Cleanup and compatibility

- Keep `/ws` temporarily as deprecated compatibility path if needed.
- Remove connection-bound assumptions from server logs and UI.
- Decide later whether `/ws` should proxy to the new task service or be removed entirely.

## Compatibility Notes

### Existing orchestrator code

The current orchestrator already exposes a task-progress callback shape:

- `run_orchestrator(task, on_step, user_id=...)`

This is compatible with the new architecture. The callback target changes from WebSocket send to event-store append + publish. This reduces refactor risk.

### Monitoring

Current `MonitoringCollector` is not a replacement for task event persistence:

- it is in-memory only
- it stores execution telemetry, not the exact user-visible event stream

It should remain a monitoring subsystem, while `TaskRunStore` becomes the source of truth for user-facing replay.

## Failure Modes and Recovery

### Browser refresh

- Task continues running.
- On reload, client restores `active_task_id`.
- SSE replay restores missing log lines.

### Network flap

- `EventSource` reconnects automatically.
- Server replays from `Last-Event-ID`.

### `uvicorn --reload` in development

- In-flight background tasks may still be interrupted by process restart.
- Persisted events still allow the client to render what was already produced.
- Full resilience across reloads requires the task runner itself to live outside the reloading process; this is out of scope for the first refactor.

### Duplicate event delivery

- Prevented by `seq` ordering and client-side dedupe by `task_id + seq`.

## Why This Design

- Matches the actual product interaction model.
- Simplifies reconnect semantics.
- Makes progress replay deterministic.
- Reduces coupling between UI connection lifecycle and task execution lifecycle.
- Leaves room for future cancellation and richer task management without committing to a WebSocket-first protocol.

## Explicit Non-Goals

- No full durable job queue in this phase.
- No distributed worker system in this phase.
- No replacement of the orchestrator planner/scheduler architecture.
- No browser-control or human-in-the-loop bidirectional channel in this phase.

If future features require rich bidirectional control, add WebSocket later for that specific surface. Do not use that future possibility as a reason to keep the main task progress path on WebSocket now.
