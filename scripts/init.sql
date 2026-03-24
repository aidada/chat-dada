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

-- Users
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    email_verified  BOOLEAN NOT NULL DEFAULT FALSE,
    display_name    TEXT NOT NULL DEFAULT '',
    avatar_url      TEXT NOT NULL DEFAULT '',
    password_hash   TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);

-- OAuth bindings
CREATE TABLE IF NOT EXISTS oauth_accounts (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider            TEXT NOT NULL,
    provider_user_id    TEXT NOT NULL,
    provider_email      TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_oauth_accounts_provider_sub
ON oauth_accounts (provider, provider_user_id);
CREATE INDEX IF NOT EXISTS idx_oauth_accounts_user_id ON oauth_accounts (user_id);

-- Session cookie store
CREATE TABLE IF NOT EXISTS user_sessions (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_token_hash  TEXT NOT NULL UNIQUE,
    expires_at          TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at          TIMESTAMPTZ,
    user_agent          TEXT NOT NULL DEFAULT '',
    ip_address          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_expires_at ON user_sessions (expires_at);

-- Per-user quota configuration
CREATE TABLE IF NOT EXISTS user_quotas (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scope                   TEXT NOT NULL DEFAULT 'default',
    enabled                 BOOLEAN NOT NULL DEFAULT TRUE,
    daily_task_limit        INTEGER,
    weekly_task_limit       INTEGER,
    monthly_task_limit      INTEGER,
    daily_token_limit       INTEGER,
    weekly_token_limit      INTEGER,
    monthly_token_limit     INTEGER,
    daily_cost_limit_usd    DOUBLE PRECISION,
    weekly_cost_limit_usd   DOUBLE PRECISION,
    monthly_cost_limit_usd  DOUBLE PRECISION,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_quotas_user_scope ON user_quotas (user_id, scope);

-- Aggregated usage events written after task completion
CREATE TABLE IF NOT EXISTS usage_events (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id         TEXT NOT NULL,
    scope           TEXT NOT NULL DEFAULT 'default',
    provider        TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    capability      TEXT NOT NULL DEFAULT 'task',
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_usd        DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_usage_events_user_scope_created_at ON usage_events (user_id, scope, created_at DESC);

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
