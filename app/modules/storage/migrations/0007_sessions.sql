-- Enrichment backlog state, sessions and answers (M7.3; ADR-117, ADR-119, ADR-120).
-- All user/AI wording is untrusted text. No provider request payloads or secrets are stored.
-- New protected tables have explicit keys and WITHOUT ROWID (ADR-104). Replacement guards
-- cover every unique key, including the one-open-session index. Caller-owned short writes
-- check version. Existing migrations and applications_delete_only_untouched are unchanged.

CREATE TABLE enrichment_questions (
    id TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('cluster', 'quality_hint', 'capture_note')),
    cluster_key TEXT,
    fact_id TEXT REFERENCES facts (id),
    capture_note_id TEXT REFERENCES capture_notes (id),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'expired')),
    cooldown_until TEXT,
    later_until TEXT,
    later_next_session INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(later_next_session) = 'integer' AND later_next_session IN (0, 1)
    ),
    not_sure_count INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(not_sure_count) = 'integer' AND not_sure_count >= 0
    ),
    asked_count INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(asked_count) = 'integer' AND asked_count >= 0
    ),
    last_outcome TEXT CHECK (last_outcome IN ('answered', 'skipped', 'not_sure', 'later')),
    last_asked_at TEXT,
    earlier_wordings TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(earlier_wordings) THEN
            json_type(earlier_wordings) = 'array' AND json_array_length(earlier_wordings) <= 5
        ELSE 0 END
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (
        (kind = 'cluster' AND cluster_key IS NOT NULL AND trim(cluster_key) <> ''
            AND fact_id IS NULL AND capture_note_id IS NULL)
        OR (kind = 'quality_hint' AND fact_id IS NOT NULL
            AND cluster_key IS NULL AND capture_note_id IS NULL)
        OR (kind = 'capture_note' AND capture_note_id IS NOT NULL
            AND cluster_key IS NULL AND fact_id IS NULL)
    ),
    CHECK (later_until IS NULL OR later_next_session = 0)
) WITHOUT ROWID;

CREATE UNIQUE INDEX enrichment_questions_cluster ON enrichment_questions (cluster_key)
WHERE cluster_key IS NOT NULL;
CREATE UNIQUE INDEX enrichment_questions_fact ON enrichment_questions (fact_id)
WHERE fact_id IS NOT NULL;
CREATE UNIQUE INDEX enrichment_questions_note ON enrichment_questions (capture_note_id)
WHERE capture_note_id IS NOT NULL;

CREATE TRIGGER enrichment_questions_never_replaced
BEFORE INSERT ON enrichment_questions
WHEN EXISTS (
    SELECT 1 FROM enrichment_questions WHERE id = NEW.id
        OR cluster_key = NEW.cluster_key OR fact_id = NEW.fact_id
        OR capture_note_id = NEW.capture_note_id
)
BEGIN
    SELECT RAISE(ABORT, 'enrichment questions are never replaced');
END;

CREATE TRIGGER enrichment_questions_identity_stable
BEFORE UPDATE ON enrichment_questions
WHEN NEW.id IS NOT OLD.id OR NEW.kind IS NOT OLD.kind
    OR NEW.cluster_key IS NOT OLD.cluster_key OR NEW.fact_id IS NOT OLD.fact_id
    OR NEW.capture_note_id IS NOT OLD.capture_note_id OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'enrichment question identity is stable');
END;

CREATE TRIGGER enrichment_questions_wordings_insert
BEFORE INSERT ON enrichment_questions
WHEN EXISTS (
    SELECT 1 FROM json_each(CASE WHEN json_valid(NEW.earlier_wordings)
        THEN NEW.earlier_wordings ELSE '[]' END) WHERE type <> 'text'
)
BEGIN
    SELECT RAISE(ABORT, 'earlier wordings must be text');
END;

CREATE TRIGGER enrichment_questions_wordings_update
BEFORE UPDATE OF earlier_wordings ON enrichment_questions
WHEN EXISTS (
    SELECT 1 FROM json_each(CASE WHEN json_valid(NEW.earlier_wordings)
        THEN NEW.earlier_wordings ELSE '[]' END) WHERE type <> 'text'
)
BEGIN
    SELECT RAISE(ABORT, 'earlier wordings must be text');
END;

CREATE TABLE sessions (
    id TEXT PRIMARY KEY NOT NULL,
    focus_kind TEXT NOT NULL CHECK (
        focus_kind IN ('most_useful', 'cluster', 'job', 'role', 'skill', 'fact', 'capture_note')
    ),
    focus_ref TEXT,
    job_id TEXT REFERENCES jobs (id),
    length INTEGER NOT NULL CHECK (typeof(length) = 'integer' AND length IN (3, 5)),
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'ended')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK ((state = 'open' AND ended_at IS NULL) OR (state = 'ended' AND ended_at IS NOT NULL)),
    CHECK ((focus_kind = 'most_useful' AND focus_ref IS NULL)
        OR (focus_kind <> 'most_useful' AND focus_ref IS NOT NULL AND trim(focus_ref) <> '')),
    CHECK (focus_kind <> 'job' OR (job_id IS NOT NULL AND job_id = focus_ref))
) WITHOUT ROWID;

CREATE UNIQUE INDEX sessions_one_open ON sessions (state) WHERE state = 'open';
CREATE INDEX sessions_job ON sessions (job_id);

CREATE TRIGGER sessions_never_replaced
BEFORE INSERT ON sessions
WHEN EXISTS (SELECT 1 FROM sessions WHERE id = NEW.id OR (state = 'open' AND NEW.state = 'open'))
BEGIN
    SELECT RAISE(ABORT, 'sessions are never replaced');
END;

CREATE TRIGGER sessions_identity_stable
BEFORE UPDATE ON sessions
WHEN NEW.id IS NOT OLD.id OR NEW.focus_kind IS NOT OLD.focus_kind
    OR NEW.focus_ref IS NOT OLD.focus_ref OR NEW.job_id IS NOT OLD.job_id
    OR NEW.length IS NOT OLD.length OR NEW.started_at IS NOT OLD.started_at
    OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'session identity is stable');
END;

CREATE TRIGGER sessions_open_collision
BEFORE UPDATE OF state ON sessions
WHEN NEW.state = 'open' AND EXISTS (SELECT 1 FROM sessions WHERE state = 'open' AND id <> OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'another session is open');
END;

CREATE TABLE session_answers (
    id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions (id),
    position INTEGER NOT NULL CHECK (typeof(position) = 'integer' AND position >= 1),
    question_id TEXT NOT NULL REFERENCES enrichment_questions (id),
    follow_up_of TEXT REFERENCES session_answers (id),
    question TEXT NOT NULL CHECK (
        typeof(question) = 'text' AND length(question) BETWEEN 1 AND 300
        AND instr(question, char(0)) = 0
    ),
    why TEXT NOT NULL CHECK (
        typeof(why) = 'text' AND length(why) BETWEEN 1 AND 200 AND instr(why, char(0)) = 0
    ),
    question_provider TEXT,
    question_model TEXT,
    question_prompt_id TEXT,
    question_prompt_version TEXT,
    fallback INTEGER NOT NULL DEFAULT 0 CHECK (typeof(fallback) = 'integer' AND fallback IN (0, 1)),
    answer_text TEXT CHECK (
        answer_text IS NULL OR (typeof(answer_text) = 'text' AND length(answer_text) <= 4000
            AND instr(answer_text, char(0)) = 0)
    ),
    outcome TEXT NOT NULL DEFAULT 'pending' CHECK (
        outcome IN ('pending', 'answered', 'skipped', 'not_sure', 'later')
    ),
    later_choice TEXT CHECK (
        later_choice IS NULL OR later_choice IN ('next_session', 'week', 'month')
        OR (length(later_choice) = 10
            AND later_choice GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]')
    ),
    draft_fact_id TEXT REFERENCES facts (id),
    missing TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(missing) THEN json_type(missing) = 'array' ELSE 0 END
    ),
    flagged_claims TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(flagged_claims) THEN json_type(flagged_claims) = 'array' ELSE 0 END
    ),
    structure_provider TEXT,
    structure_model TEXT,
    structure_prompt_id TEXT,
    structure_prompt_version TEXT,
    question_task_id TEXT REFERENCES tasks (id) ON DELETE SET NULL,
    structure_task_id TEXT REFERENCES tasks (id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    UNIQUE (session_id, position),
    CHECK ((outcome = 'later') = (later_choice IS NOT NULL))
) WITHOUT ROWID;

CREATE INDEX session_answers_draft ON session_answers (draft_fact_id);
CREATE INDEX session_answers_question ON session_answers (question_id);
CREATE INDEX session_answers_follow_up ON session_answers (follow_up_of);
CREATE INDEX session_answers_question_task ON session_answers (question_task_id);
CREATE INDEX session_answers_structure_task ON session_answers (structure_task_id);

CREATE TRIGGER session_answers_never_replaced
BEFORE INSERT ON session_answers
WHEN EXISTS (
    SELECT 1 FROM session_answers WHERE id = NEW.id
        OR (session_id = NEW.session_id AND position = NEW.position)
)
BEGIN
    SELECT RAISE(ABORT, 'session answers are never replaced');
END;

CREATE TRIGGER session_answers_identity_stable
BEFORE UPDATE ON session_answers
WHEN NEW.id IS NOT OLD.id OR NEW.session_id IS NOT OLD.session_id
    OR NEW.position IS NOT OLD.position OR NEW.question_id IS NOT OLD.question_id
    OR NEW.follow_up_of IS NOT OLD.follow_up_of OR NEW.created_at IS NOT OLD.created_at
    OR NEW.question IS NOT OLD.question OR NEW.why IS NOT OLD.why
    OR NEW.question_provider IS NOT OLD.question_provider OR NEW.question_model IS NOT OLD.question_model
    OR NEW.question_prompt_id IS NOT OLD.question_prompt_id
    OR NEW.question_prompt_version IS NOT OLD.question_prompt_version OR NEW.fallback IS NOT OLD.fallback
BEGIN
    SELECT RAISE(ABORT, 'session answer identity and question are stable');
END;

CREATE TRIGGER session_answers_follow_up_scope
BEFORE INSERT ON session_answers
WHEN NEW.follow_up_of IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM session_answers p WHERE p.id = NEW.follow_up_of
        AND p.session_id = NEW.session_id AND p.question_id = NEW.question_id
        AND p.position < NEW.position
)
BEGIN
    SELECT RAISE(ABORT, 'follow-up must follow its item in the same session');
END;

CREATE TRIGGER session_answers_missing_insert
BEFORE INSERT ON session_answers
WHEN EXISTS (
    SELECT 1 FROM json_each(CASE WHEN json_valid(NEW.missing) THEN NEW.missing ELSE '[]' END)
    WHERE type <> 'text' OR value NOT IN ('result', 'number', 'scale', 'own_part', 'context')
)
BEGIN
    SELECT RAISE(ABORT, 'unknown missing answer part');
END;

CREATE TRIGGER session_answers_missing_update
BEFORE UPDATE OF missing ON session_answers
WHEN EXISTS (
    SELECT 1 FROM json_each(CASE WHEN json_valid(NEW.missing) THEN NEW.missing ELSE '[]' END)
    WHERE type <> 'text' OR value NOT IN ('result', 'number', 'scale', 'own_part', 'context')
)
BEGIN
    SELECT RAISE(ABORT, 'unknown missing answer part');
END;
