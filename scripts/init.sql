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

-- Conversations: lightweight metadata; messages live in task_events via task_runs.
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '新对话',
    pinned      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations (user_id);

-- Link tasks to conversations
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'task_runs' AND column_name = 'conversation_id'
    ) THEN
        ALTER TABLE task_runs ADD COLUMN conversation_id TEXT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_task_runs_conversation ON task_runs (conversation_id);

-- Conversation context summary cache
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='conversations' AND column_name='context_summary') THEN
    ALTER TABLE conversations ADD COLUMN context_summary TEXT DEFAULT '';
    ALTER TABLE conversations ADD COLUMN summary_through_seq INTEGER DEFAULT 0;
  END IF;
END $$;

-- pgvector extension + embedding column for semantic retrieval
CREATE EXTENSION IF NOT EXISTS vector;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='task_events' AND column_name='embedding') THEN
    ALTER TABLE task_events ADD COLUMN embedding vector(1536);
  END IF;
END $$;
