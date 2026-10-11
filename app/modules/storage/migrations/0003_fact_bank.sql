-- Fact bank (M3.2; spec 2.7, 3.1, 3.2, 3.5; ADR-027, ADR-038, ADR-089 items 5, 6, 8, 11).
-- Every table M3 needs, in one migration: roles, CV imports, facts with their append-only
-- versions and events, skills, capture notes, Refine with AI conversations, dismissed
-- near-duplicate pairs and the full-text index over facts. Same conventions as 0001 and 0002:
-- UTC ISO 8601 text times from the Clock, no STRICT tables, so whole-number columns check
-- typeof() themselves; mutable tables carry created_at, updated_at and version (ADR-027).
-- Names that may grow in later milestones (version sources, event types) are non-blank free text
-- validated by the domain, because a CHECK can't change without a table rebuild. Nothing is
-- seeded: the skills list starts empty (ADR-089 item 11).
--
-- Three numbers to keep apart: facts.version is the row's optimistic-concurrency counter,
-- facts.number is the 12 in the reference F12@3, and fact_versions.version is the 3.
--
-- fact_events are rows of a fact's history, not calendar events, so the rule that events also
-- store an IANA time zone doesn't apply to them.

-- A job or other position that facts belong to. The CV import creates draft roles; a draft role
-- is confirmed with its first confirmed fact (M3.3). A role needs an employer or a title; the
-- other may be ''. Dates are 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD' text, since CVs often give only a
-- year or a month, or NULL when unknown or still current; the domain checks they are real dates.
-- A role that facts point at can't be deleted.
CREATE TABLE roles (
    id TEXT PRIMARY KEY NOT NULL,
    employer TEXT NOT NULL,
    title TEXT NOT NULL,
    start_date TEXT CHECK (
        start_date IS NULL
        OR start_date GLOB '[0-9][0-9][0-9][0-9]'
        OR start_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'
        OR start_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'
    ),
    end_date TEXT CHECK (
        end_date IS NULL
        OR end_date GLOB '[0-9][0-9][0-9][0-9]'
        OR end_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'
        OR end_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'
    ),
    status TEXT NOT NULL CHECK (status IN ('draft', 'confirmed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (
        trim(employer, char(32, 9, 10, 13)) <> '' OR trim(title, char(32, 9, 10, 13)) <> ''
    )
);

-- One uploaded or pasted CV (M3.4). The file is kept under <data folder>/imports/ with a name
-- the app generates, never the client's file name and never a path (spec 2.9 rule 5): letters,
-- digits, '.', '_' and '-' only, not starting with a dot. kind is what the content was sniffed
-- as (app.modules.extract.KINDS). state is 'uploaded' until the extract_cv task has stored its
-- drafts; a failed or cancelled attempt stays 'uploaded' and its reason lives on the task.
-- Deleting the task only clears the link.
CREATE TABLE cv_imports (
    id TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('pdf', 'docx', 'text')),
    stored_name TEXT NOT NULL UNIQUE CHECK (
        stored_name <> ''
        AND stored_name NOT GLOB '*[^A-Za-z0-9._-]*'
        AND stored_name NOT GLOB '.*'
    ),
    state TEXT NOT NULL CHECK (state IN ('uploaded', 'done')),
    task_id TEXT REFERENCES tasks (id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
);

-- A fact or a story (situation, action, result) about the user. The content lives in
-- fact_versions; this row holds what can change without a new version: status (spec 3.2),
-- privacy (never_use facts are never sent to AI, generalize ones have their names generalized;
-- ADR-089 item 6), role, tags (a JSON array) and dates (as in roles). number is the short,
-- stable number facts are cited by; the trigger below keeps it from changing.
-- current_version_id always names a version of this same fact. The key is deferred because the
-- fact row is written first and its first version later in the same transaction; the commit
-- checks it. A draft merged into another fact is closed as discarded with merged_into_fact_id
-- set, and merge undo clears the link and restores the draft in one UPDATE (M3.3).
-- Removing a CV import only clears cv_import_id; its facts stay.
-- A fact is never deleted: its versions can't be, and they point at it.
CREATE TABLE facts (
    id TEXT PRIMARY KEY NOT NULL,
    number INTEGER NOT NULL UNIQUE CHECK (typeof(number) = 'integer' AND number >= 1),
    type TEXT NOT NULL CHECK (type IN ('fact', 'story')),
    status TEXT NOT NULL CHECK (status IN ('draft', 'confirmed', 'discarded', 'archived')),
    role_id TEXT REFERENCES roles (id),
    privacy TEXT NOT NULL DEFAULT 'normal' CHECK (
        privacy IN ('normal', 'never_use', 'generalize')
    ),
    tags TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(tags) THEN json_type(tags) = 'array' ELSE 0 END
    ),
    start_date TEXT CHECK (
        start_date IS NULL
        OR start_date GLOB '[0-9][0-9][0-9][0-9]'
        OR start_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'
        OR start_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'
    ),
    end_date TEXT CHECK (
        end_date IS NULL
        OR end_date GLOB '[0-9][0-9][0-9][0-9]'
        OR end_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'
        OR end_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'
    ),
    current_version_id TEXT NOT NULL,
    merged_into_fact_id TEXT REFERENCES facts (id) CHECK (
        merged_into_fact_id IS NULL OR merged_into_fact_id <> id
    ),
    cv_import_id TEXT REFERENCES cv_imports (id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    FOREIGN KEY (id, current_version_id) REFERENCES fact_versions (fact_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (merged_into_fact_id IS NULL OR status = 'discarded')
);

-- The library filters by status and role, and drafts are grouped by their import (M3.5).
CREATE INDEX facts_status ON facts (status);
CREATE INDEX facts_role_id ON facts (role_id);
CREATE INDEX facts_cv_import_id ON facts (cv_import_id);

-- Answers already given cite F<number>@<version>, and the search index is keyed by the number.
CREATE TRIGGER facts_number_never_changes
BEFORE UPDATE OF number ON facts
WHEN NEW.number IS NOT OLD.number
BEGIN
    SELECT RAISE(ABORT, 'facts.number never changes');
END;

-- INSERT OR REPLACE removes the row it replaces without running delete triggers or the trigger
-- above, so it could renumber a fact. This guard refuses an insert that would take the place of
-- an existing fact; a fact changes only through UPDATE.
CREATE TRIGGER facts_never_replaced
BEFORE INSERT ON facts
WHEN EXISTS (SELECT 1 FROM facts WHERE id = NEW.id OR number = NEW.number)
BEGIN
    SELECT RAISE(ABORT, 'facts rows are never replaced');
END;

-- The content of a fact at one point in time: a plain text, story parts, or both, with at least
-- one of them non-blank. Every edit adds a row with the next version number (AC-M3.3); rows are
-- never changed or deleted, so the table has no updated_at. The triggers below enforce it: two
-- refuse UPDATE and DELETE, and an insert guard refuses a row that would take the place of an
-- existing one, because INSERT OR REPLACE removes the old row without running delete triggers.
-- source says where the wording came from ('manual', 'cv_import', 'refine', 'merge', ...) and
-- original_answer keeps the user's or the CV's own words behind it. provider, model, prompt_id
-- and prompt_version record what generated the wording and are NULL for a fact the user typed
-- (ADR-089 item 8). needs_check marks a CV-import draft whose quote or numbers weren't found in
-- the CV text (ADR-089 item 5). UNIQUE (fact_id, id) is the parent key of the three foreign
-- keys that tie a version to its own fact (facts, fact_events and refine_sessions).
CREATE TABLE fact_versions (
    id TEXT PRIMARY KEY NOT NULL,
    fact_id TEXT NOT NULL REFERENCES facts (id),
    version INTEGER NOT NULL CHECK (typeof(version) = 'integer' AND version >= 1),
    text TEXT,
    situation TEXT,
    action TEXT,
    result TEXT,
    source TEXT NOT NULL CHECK (trim(source, char(32, 9, 10, 13)) <> ''),
    original_answer TEXT,
    provider TEXT,
    model TEXT,
    prompt_id TEXT,
    prompt_version TEXT,
    needs_check INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(needs_check) = 'integer' AND needs_check IN (0, 1)
    ),
    created_at TEXT NOT NULL,
    UNIQUE (fact_id, version),
    UNIQUE (fact_id, id),
    CHECK (
        trim(
            coalesce(text, '') || coalesce(situation, '') || coalesce(action, '')
            || coalesce(result, ''),
            char(32, 9, 10, 13)
        ) <> ''
    )
);

CREATE TRIGGER fact_versions_never_updated
BEFORE UPDATE ON fact_versions
BEGIN
    SELECT RAISE(ABORT, 'fact_versions rows are never changed');
END;

CREATE TRIGGER fact_versions_never_deleted
BEFORE DELETE ON fact_versions
BEGIN
    SELECT RAISE(ABORT, 'fact_versions rows are never deleted');
END;

CREATE TRIGGER fact_versions_never_replaced
BEFORE INSERT ON fact_versions
WHEN EXISTS (
    SELECT 1 FROM fact_versions
    WHERE id = NEW.id OR (fact_id = NEW.fact_id AND version = NEW.version)
)
BEGIN
    SELECT RAISE(ABORT, 'fact_versions rows are never replaced');
END;

-- What happened to a fact besides its wording: created, confirmed, discarded, restored,
-- archived, privacy changed, merged and so on, written in the same transaction as the change
-- (M3.3). type is non-blank free text validated by the domain; fact_version_id is the version
-- the event concerns, if any, and always a version of the event's own fact; detail is optional
-- JSON such as the old and new privacy. Like versions, events are history: only ever inserted,
-- so there is no updated_at or version, and the triggers below refuse UPDATE, DELETE and an
-- insert that would replace an existing event (INSERT OR REPLACE).
CREATE TABLE fact_events (
    id TEXT PRIMARY KEY NOT NULL,
    fact_id TEXT NOT NULL REFERENCES facts (id),
    type TEXT NOT NULL CHECK (trim(type, char(32, 9, 10, 13)) <> ''),
    fact_version_id TEXT,
    detail TEXT CHECK (detail IS NULL OR json_valid(detail)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (fact_id, fact_version_id) REFERENCES fact_versions (fact_id, id)
);

-- A fact's history, oldest first.
CREATE INDEX fact_events_fact_id_created_at ON fact_events (fact_id, created_at);

CREATE TRIGGER fact_events_never_updated
BEFORE UPDATE ON fact_events
BEGIN
    SELECT RAISE(ABORT, 'fact_events rows are never changed');
END;

CREATE TRIGGER fact_events_never_deleted
BEFORE DELETE ON fact_events
BEGIN
    SELECT RAISE(ABORT, 'fact_events rows are never deleted');
END;

CREATE TRIGGER fact_events_never_replaced
BEFORE INSERT ON fact_events
WHEN EXISTS (SELECT 1 FROM fact_events WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'fact_events rows are never replaced');
END;

-- The user's own list of skill names. name is shown; normalized_name is what the domain reduces
-- a name to, and being unique it keeps "Forecasting" and "FORECASTING" from both existing;
-- aliases is a JSON array of other spellings that mean the same skill (M3.3). A skill that facts
-- are linked to can't be deleted.
CREATE TABLE skills (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL CHECK (trim(name, char(32, 9, 10, 13)) <> ''),
    normalized_name TEXT NOT NULL UNIQUE CHECK (
        trim(normalized_name, char(32, 9, 10, 13)) <> ''
    ),
    aliases TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(aliases) THEN json_type(aliases) = 'array' ELSE 0 END
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
);

-- Which skills a fact shows. A link exists or doesn't; rows are inserted and deleted, so there
-- are no timestamps or version.
CREATE TABLE fact_skills (
    fact_id TEXT NOT NULL REFERENCES facts (id),
    skill_id TEXT NOT NULL REFERENCES skills (id),
    PRIMARY KEY (fact_id, skill_id)
);

-- The library's skill filter, and the facts to re-index when a skill is renamed.
CREATE INDEX fact_skills_skill_id ON fact_skills (skill_id);

-- Quick notes for later (M3.7). "Turn into a fact" sets resolved_fact_id; delete with undo sets
-- and clears deleted_at, and a note may be removed for good afterwards.
CREATE TABLE capture_notes (
    id TEXT PRIMARY KEY NOT NULL,
    text TEXT NOT NULL CHECK (trim(text, char(32, 9, 10, 13)) <> ''),
    resolved_fact_id TEXT REFERENCES facts (id),
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
);

-- One Refine with AI conversation about a fact (M3.8; ADR-089 item 5). base_version_id is the
-- version of that same fact the conversation started from. turns is a JSON array of the
-- questions asked and the user's answers, with at most three answer rounds; suggestion is the
-- latest suggested wording as JSON; flagged_claims lists what the honesty check found in it that
-- isn't in the allowed sources, and acknowledged_claims the ones the user asserted are true.
-- provider, model, prompt_id and prompt_version describe the latest turn. A turn runs as the
-- task refine_fact:<fact id>, so a fact has at most one open conversation, which the index below
-- enforces. Deleting the task only clears the link.
CREATE TABLE refine_sessions (
    id TEXT PRIMARY KEY NOT NULL,
    fact_id TEXT NOT NULL REFERENCES facts (id),
    base_version_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'accepted', 'discarded')),
    answer_rounds INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(answer_rounds) = 'integer' AND answer_rounds BETWEEN 0 AND 3
    ),
    turns TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(turns) THEN json_type(turns) = 'array' ELSE 0 END
    ),
    suggestion TEXT CHECK (suggestion IS NULL OR json_valid(suggestion)),
    flagged_claims TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(flagged_claims) THEN json_type(flagged_claims) = 'array' ELSE 0 END
    ),
    acknowledged_claims TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE
            WHEN json_valid(acknowledged_claims) THEN json_type(acknowledged_claims) = 'array'
            ELSE 0
        END
    ),
    provider TEXT,
    model TEXT,
    prompt_id TEXT,
    prompt_version TEXT,
    task_id TEXT REFERENCES tasks (id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    FOREIGN KEY (fact_id, base_version_id) REFERENCES fact_versions (fact_id, id)
);

CREATE UNIQUE INDEX refine_sessions_open_fact_id ON refine_sessions (fact_id)
    WHERE state = 'open';

-- Pairs of facts the user said are not duplicates, so "Find possible duplicates" doesn't
-- suggest them again (M3.7). Each pair is stored once, with the smaller id first. Rows are only
-- inserted, so there is no updated_at or version.
CREATE TABLE fact_pair_dismissals (
    fact_a_id TEXT NOT NULL REFERENCES facts (id),
    fact_b_id TEXT NOT NULL REFERENCES facts (id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (fact_a_id, fact_b_id),
    CHECK (fact_a_id < fact_b_id)
);

-- Full-text search over facts (AC-M3.6): one entry per fact that isn't discarded, holding its
-- current version's text and story parts, its role's employer and title and its skill names.
-- The entry's rowid is facts.number, so an entry is found and replaced without a scan; fact_id
-- is stored for joins and isn't searchable. Search text from the user must be passed as quoted
-- tokens (M3.3).
CREATE VIRTUAL TABLE facts_fts USING fts5 (
    fact_id UNINDEXED,
    text,
    situation,
    action,
    result,
    role,
    skills
);

-- What each fact's search entry should hold right now. A discarded fact, or a fact whose first
-- version isn't written yet, has no row here and so no entry.
CREATE VIEW facts_fts_source AS
SELECT
    f.number AS number,
    f.id AS fact_id,
    v.text AS text,
    v.situation AS situation,
    v.action AS action,
    v.result AS result,
    (SELECT r.employer || ' ' || r.title FROM roles AS r WHERE r.id = f.role_id) AS role,
    (
        SELECT group_concat(s.name, ' ')
        FROM fact_skills AS fs
        JOIN skills AS s ON s.id = fs.skill_id
        WHERE fs.fact_id = f.id
    ) AS skills
FROM facts AS f
JOIN fact_versions AS v ON v.id = f.current_version_id
WHERE f.status <> 'discarded';

-- Every trigger below does the same two steps for the facts a change touches: delete their
-- entries, then insert what facts_fts_source holds for them now.

-- A fact row is written before its first version and can't replace an existing fact, so
-- inserting one never changes the index. A new version is searchable once its fact points at
-- it: at once for a first version, whose fact row already names it, and through the facts
-- trigger below for an edit.
CREATE TRIGGER facts_fts_after_version_insert
AFTER INSERT ON fact_versions
BEGIN
    DELETE FROM facts_fts
    WHERE rowid IN (
        SELECT number FROM facts WHERE id = NEW.fact_id AND current_version_id = NEW.id
    );
    INSERT INTO facts_fts (rowid, fact_id, text, situation, action, result, role, skills)
    SELECT number, fact_id, text, situation, action, result, role, skills
    FROM facts_fts_source
    WHERE fact_id IN (
        SELECT id FROM facts WHERE id = NEW.fact_id AND current_version_id = NEW.id
    );
END;

-- An edit (new current version), a move to another role or to none, and a discard or its undo.
-- Other status changes leave the entry as it is.
CREATE TRIGGER facts_fts_after_fact_update
AFTER UPDATE OF current_version_id, role_id, status ON facts
WHEN NEW.current_version_id IS NOT OLD.current_version_id
    OR NEW.role_id IS NOT OLD.role_id
    OR (NEW.status IS NOT OLD.status AND 'discarded' IN (OLD.status, NEW.status))
BEGIN
    DELETE FROM facts_fts WHERE rowid = NEW.number;
    INSERT INTO facts_fts (rowid, fact_id, text, situation, action, result, role, skills)
    SELECT number, fact_id, text, situation, action, result, role, skills
    FROM facts_fts_source
    WHERE fact_id = NEW.id;
END;

-- Renaming a role reaches every fact of that role.
CREATE TRIGGER facts_fts_after_role_update
AFTER UPDATE OF employer, title ON roles
WHEN NEW.employer IS NOT OLD.employer OR NEW.title IS NOT OLD.title
BEGIN
    DELETE FROM facts_fts WHERE rowid IN (SELECT number FROM facts WHERE role_id = NEW.id);
    INSERT INTO facts_fts (rowid, fact_id, text, situation, action, result, role, skills)
    SELECT number, fact_id, text, situation, action, result, role, skills
    FROM facts_fts_source
    WHERE fact_id IN (SELECT id FROM facts WHERE role_id = NEW.id);
END;

-- A skill linked to a fact, unlinked from it, or a link moved to another fact or skill.
CREATE TRIGGER facts_fts_after_fact_skill_insert
AFTER INSERT ON fact_skills
BEGIN
    DELETE FROM facts_fts WHERE rowid IN (SELECT number FROM facts WHERE id = NEW.fact_id);
    INSERT INTO facts_fts (rowid, fact_id, text, situation, action, result, role, skills)
    SELECT number, fact_id, text, situation, action, result, role, skills
    FROM facts_fts_source
    WHERE fact_id = NEW.fact_id;
END;

CREATE TRIGGER facts_fts_after_fact_skill_delete
AFTER DELETE ON fact_skills
BEGIN
    DELETE FROM facts_fts WHERE rowid IN (SELECT number FROM facts WHERE id = OLD.fact_id);
    INSERT INTO facts_fts (rowid, fact_id, text, situation, action, result, role, skills)
    SELECT number, fact_id, text, situation, action, result, role, skills
    FROM facts_fts_source
    WHERE fact_id = OLD.fact_id;
END;

CREATE TRIGGER facts_fts_after_fact_skill_update
AFTER UPDATE ON fact_skills
BEGIN
    DELETE FROM facts_fts
    WHERE rowid IN (SELECT number FROM facts WHERE id IN (OLD.fact_id, NEW.fact_id));
    INSERT INTO facts_fts (rowid, fact_id, text, situation, action, result, role, skills)
    SELECT number, fact_id, text, situation, action, result, role, skills
    FROM facts_fts_source
    WHERE fact_id IN (OLD.fact_id, NEW.fact_id);
END;

-- Renaming a skill reaches every fact linked to it.
CREATE TRIGGER facts_fts_after_skill_update
AFTER UPDATE OF name ON skills
WHEN NEW.name IS NOT OLD.name
BEGIN
    DELETE FROM facts_fts
    WHERE rowid IN (
        SELECT f.number
        FROM fact_skills AS fs
        JOIN facts AS f ON f.id = fs.fact_id
        WHERE fs.skill_id = NEW.id
    );
    INSERT INTO facts_fts (rowid, fact_id, text, situation, action, result, role, skills)
    SELECT number, fact_id, text, situation, action, result, role, skills
    FROM facts_fts_source
    WHERE fact_id IN (SELECT fact_id FROM fact_skills WHERE skill_id = NEW.id);
END;
