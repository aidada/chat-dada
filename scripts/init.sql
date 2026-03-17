-- PostgreSQL schema for chat-dada
-- Idempotent: all statements use IF NOT EXISTS

CREATE TABLE IF NOT EXISTS task_runs (
    task_id             TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    status              TEXT NOT NULL,
    task_text           TEXT NOT NULL,
    mode                TEXT NOT NULL DEFAULT 'auto',
    thinking_level      TEXT NOT NULL,
    route_name          TEXT,
    route_reason        TEXT,
    route_confidence    REAL,
    request_payload     JSONB NOT NULL,
    result_text         TEXT,
    error_text          TEXT,
    pending_question    JSONB,
    created_at          TIMESTAMPTZ NOT NULL,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_runs_user_id ON task_runs (user_id);
CREATE INDEX IF NOT EXISTS idx_task_runs_status  ON task_runs (status);
CREATE INDEX IF NOT EXISTS idx_task_runs_created ON task_runs (created_at DESC);

CREATE TABLE IF NOT EXISTS task_events (
    task_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (task_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_task_events_task_seq ON task_events (task_id, seq);
