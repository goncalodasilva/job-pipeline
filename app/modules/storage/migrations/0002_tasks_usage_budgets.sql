-- Task runner and budgets (M2.5; spec 2.7; ADR-027, ADR-038, ADR-076 items 4, 5, 7; ADR-078).
-- Adds what 0001 deferred to M2: the unique index on active tasks.dedupe_key and the
-- usage_records.task_id link. Also records the service and token counts on usage records,
-- the latest provider-reported usage per service and window, and the budget warnings already
-- shown. Same conventions as 0001: UTC ISO 8601 text times from the Clock, no STRICT tables, so
-- whole-number and real columns check typeof() themselves.

-- At most one queued or running task per dedupe key (AC-M2.1). Finished tasks keep their key, so
-- the same work can be enqueued again, and a retry that moves a finished row back to queued is
-- refused while another task with that key is active (ADR-076 item 11). Nothing writes tasks
-- before 0002, so an upgraded 0001 database can't hold duplicate active keys.
CREATE UNIQUE INDEX tasks_active_dedupe_key ON tasks (dedupe_key)
    WHERE status IN ('queued', 'running');

-- The runner claims status = 'queued' ORDER BY updated_at (a retry re-queues the row and sets
-- updated_at), and the tasks panel lists by status ordered by updated_at; both use the first index.
-- Recent tasks by creation use the second.
CREATE INDEX tasks_status_updated_at ON tasks (status, updated_at);
CREATE INDEX tasks_created_at ON tasks (created_at);

-- Usage records link to the task that made the call (AC-M2.6). Deleting a task keeps its usage
-- counted, only the link is cleared. SET NULL doesn't bump the row's version, which is fine:
-- usage rows are append-only records. Rows written before 0002 keep NULL in every new column.
ALTER TABLE usage_records ADD COLUMN task_id TEXT REFERENCES tasks (id) ON DELETE SET NULL;

-- The budget a record counts against (ADR-076 item 4). The schema only requires a non-blank
-- name, as in provider_usage_snapshots; the domain validates the set of services. ADD COLUMN can't
-- be NOT NULL, so the trigger below makes every new row name its service while 0001-era rows
-- keep NULL.
ALTER TABLE usage_records ADD COLUMN service TEXT CHECK (
    service IS NULL OR trim(service, char(32, 9, 10, 13)) <> ''
);

CREATE TRIGGER usage_records_service_required
BEFORE INSERT ON usage_records
WHEN NEW.service IS NULL OR trim(NEW.service, char(32, 9, 10, 13)) = ''
BEGIN
    SELECT RAISE(ABORT, 'usage_records.service is required');
END;

-- Token counts are recorded and shown in the breakdown, but budgets count requests only
-- (ADR-076 item 5). NULL while a call is in flight or when the provider reports none.
ALTER TABLE usage_records ADD COLUMN input_tokens INTEGER CHECK (
    input_tokens IS NULL OR (typeof(input_tokens) = 'integer' AND input_tokens >= 0)
);
ALTER TABLE usage_records ADD COLUMN output_tokens INTEGER CHECK (
    output_tokens IS NULL OR (typeof(output_tokens) = 'integer' AND output_tokens >= 0)
);

-- Usage breakdown by task, and budget counts per service within a cycle.
CREATE INDEX usage_records_task_id ON usage_records (task_id);
CREATE INDEX usage_records_service_requested_at ON usage_records (service, requested_at);

-- Latest provider-reported usage, one row per (service, window), updated in place with no
-- history (ADR-078, user decision d). A "requests" snapshot carries a count, a "fraction" one a
-- utilization from 0 to 1. Counts stay within SQLite's INTEGER range: a value past it becomes
-- REAL and fails the typeof check. NaN is stored as NULL, so a fraction without its value is
-- refused. No raw payloads, session ids, uuids, status, model names or free text are stored.
-- "window" is quoted because it is an SQL keyword, and "limit" is stored as limit_count.
CREATE TABLE provider_usage_snapshots (
    id TEXT PRIMARY KEY NOT NULL,
    service TEXT NOT NULL CHECK (trim(service, char(32, 9, 10, 13)) <> ''),
    "window" TEXT NOT NULL CHECK (trim("window", char(32, 9, 10, 13)) <> ''),
    unit TEXT NOT NULL CHECK (unit IN ('requests', 'fraction')),
    used INTEGER CHECK (used IS NULL OR (typeof(used) = 'integer' AND used >= 0)),
    limit_count INTEGER CHECK (
        limit_count IS NULL OR (typeof(limit_count) = 'integer' AND limit_count >= 0)
    ),
    utilization REAL CHECK (
        utilization IS NULL OR (typeof(utilization) = 'real' AND utilization BETWEEN 0.0 AND 1.0)
    ),
    resets_at TEXT,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (
        (unit = 'requests' AND used IS NOT NULL)
        OR (unit = 'fraction' AND utilization IS NOT NULL)
    ),
    UNIQUE (service, "window")
);

-- Each budget warning is shown once per service, cycle and threshold (ADR-076 item 7). A row per
-- notice with a unique key avoids read-modify-write races on settings JSON. Rows are only ever
-- inserted, never updated, so the table has no updated_at or version (ADR-027 covers mutable
-- tables); shown_at is when the row was written. Cycles start at local midnight (ADR-076 item 8),
-- so cycle_start is the local calendar date 'YYYY-MM-DD': canonical and free of time zones.
-- service is non-blank free text; the domain validates the set (ADR-076 item 4).
CREATE TABLE budget_notices (
    id TEXT PRIMARY KEY NOT NULL,
    service TEXT NOT NULL CHECK (trim(service, char(32, 9, 10, 13)) <> ''),
    cycle_start TEXT NOT NULL CHECK (
        length(cycle_start) = 10
        AND cycle_start GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'
    ),
    threshold TEXT NOT NULL CHECK (threshold IN ('first', 'second')),
    shown_at TEXT NOT NULL,
    UNIQUE (service, cycle_start, threshold)
);
