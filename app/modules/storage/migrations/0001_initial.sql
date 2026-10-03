-- Initial schema (M0.12; spec 2.7; ADR-027, ADR-038).
-- Times are UTC ISO 8601 text written by the app's Clock. Mutable tables carry created_at,
-- updated_at and version for optimistic concurrency. M2 adds the unique index on active
-- tasks.dedupe_key and the usage_records.task_id link.

-- The runner creates this table before applying 0001; declared here so the schema is complete.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL CHECK (json_valid(value)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
);

CREATE TABLE setup_state (
    step_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'skipped')),
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')
    ),
    dedupe_key TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    heartbeat_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    input_ref TEXT,
    result_ref TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
);

CREATE INDEX tasks_status ON tasks (status);

CREATE TABLE usage_records (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    task_type TEXT NOT NULL,
    tier TEXT,
    units INTEGER NOT NULL DEFAULT 0 CHECK (units >= 0),
    requested_at TEXT NOT NULL,
    success INTEGER CHECK (success IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
);

CREATE INDEX usage_records_provider_requested_at ON usage_records (provider, requested_at);
