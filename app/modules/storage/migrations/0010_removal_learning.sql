-- ADR-131: keep removal and restore events, with optimistic versions on rules.
CREATE TABLE job_removals (
    job_id TEXT NOT NULL REFERENCES jobs (id),
    job_version INTEGER NOT NULL CHECK (typeof(job_version) = 'integer' AND job_version >= 1),
    event TEXT NOT NULL CHECK (event IN ('remove', 'restore')),
    reason TEXT CHECK (
        reason IS NULL OR reason IN (
            'too_junior', 'too_senior', 'location', 'salary', 'industry', 'company',
            'not_interested', 'other'
        )
    ),
    text TEXT,
    removed_at TEXT NOT NULL,
    backfilled INTEGER NOT NULL DEFAULT 0 CHECK (backfilled IN (0, 1)),
    PRIMARY KEY (job_id, job_version),
    CHECK (event = 'remove' OR (reason IS NULL AND text IS NULL AND backfilled = 0))
) WITHOUT ROWID;

INSERT INTO job_removals (job_id, job_version, event, reason, text, removed_at, backfilled)
SELECT id, version, 'remove', removal_reason, removal_text, updated_at, 1
FROM jobs WHERE triage_state = 'removed' AND removal_reason IS NOT NULL;

-- Capture a transition only after the versioned write has matched a row.
CREATE TRIGGER jobs_removal_history
AFTER UPDATE OF triage_state ON jobs
WHEN (OLD.triage_state = 'removed') <> (NEW.triage_state = 'removed')
BEGIN
    INSERT INTO job_removals (job_id, job_version, event, reason, text, removed_at)
    VALUES (
        NEW.id, NEW.version,
        CASE NEW.triage_state WHEN 'removed' THEN 'remove' ELSE 'restore' END,
        CASE NEW.triage_state WHEN 'removed' THEN NEW.removal_reason END,
        CASE NEW.triage_state WHEN 'removed' THEN NEW.removal_text END,
        NEW.updated_at
    );
END;

CREATE TRIGGER job_removals_no_replace
BEFORE INSERT ON job_removals
WHEN EXISTS (
    SELECT 1 FROM job_removals WHERE job_id = NEW.job_id AND job_version = NEW.job_version
)
BEGIN
    SELECT RAISE(ABORT, 'removal history is append-only');
END;

CREATE TRIGGER job_removals_no_update
BEFORE UPDATE ON job_removals
BEGIN
    SELECT RAISE(ABORT, 'removal history is append-only');
END;

CREATE TRIGGER job_removals_no_delete
BEFORE DELETE ON job_removals
BEGIN
    SELECT RAISE(ABORT, 'removal history is append-only');
END;

CREATE TABLE removal_rules (
    id TEXT PRIMARY KEY NOT NULL,
    field TEXT NOT NULL CHECK (
        field IN ('seniority', 'location', 'work_mode', 'minimum_salary', 'industry', 'company')
    ),
    condition TEXT NOT NULL CHECK (
        CASE WHEN json_valid(condition) THEN json_type(condition) = 'object' ELSE 0 END
    ),
    mode TEXT NOT NULL DEFAULT 'flag' CHECK (mode IN ('flag', 'hide')),
    evidence_job_ids TEXT NOT NULL CHECK (
        CASE WHEN json_valid(evidence_job_ids) THEN json_type(evidence_job_ids) = 'array'
             ELSE 0 END
    ),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    origin TEXT NOT NULL CHECK (origin IN ('pattern', 'ai')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
);

CREATE TABLE removal_suggestion_dismissals (
    signature TEXT PRIMARY KEY NOT NULL,
    evidence_count INTEGER NOT NULL CHECK (typeof(evidence_count) = 'integer' AND evidence_count > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
);

-- ADR-137: checked proposals only, saved atomically with task success.
CREATE TABLE removal_theme_runs (
    task_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks (id),
    themes TEXT NOT NULL CHECK (
        CASE WHEN json_valid(themes) THEN json_type(themes) = 'array' ELSE 0 END
    ),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_id TEXT NOT NULL CHECK (prompt_id = 'group_removal_reasons'),
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX removal_theme_runs_latest ON removal_theme_runs (created_at DESC);
