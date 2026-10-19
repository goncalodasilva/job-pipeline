-- Jobs (M4.2; spec 2.7, 3.1, 3.3; ADR-027, ADR-064, ADR-098 items 3, 5, 6, 8).
-- Every table M4 needs, in one migration: companies, jobs with their filter columns and triage
-- state, where each job was found, the append-only AI summaries and fit assessments with the
-- requirements a summary lists, the controlled lists the summary is normalized to (seeded
-- here), unknown values waiting to be mapped, dismissed duplicate pairs, saved inbox views and
-- the full-text index over jobs. Same conventions as 0001 to 0003: UTC ISO 8601 text times
-- from the Clock, no STRICT tables, so whole-number columns check typeof() themselves; mutable
-- tables carry created_at, updated_at and version (ADR-027). Values that may grow in later
-- milestones (filter values, salary currency and period, company size) are free text validated
-- by the domain, because a CHECK can't change without a table rebuild; the lists the domain
-- branches on (triage states, fit bands, removal reasons, source and requirement kinds) are
-- CHECKed.
--
-- Job text, page snapshots, summaries and company details come from the web or the AI and are
-- untrusted data (spec 3.3): stored as text, never run, and escaped when shown. Links stored
-- here are http or https only, so none can become a javascript: or file: link on a page.
--
-- Two kinds of dates to keep apart: *_at columns are UTC instants; jobs.snooze_until and
-- jobs.posted_date are calendar dates ('YYYY-MM-DD'), and a snooze ends on the user's local
-- date (app.domain.jobs.effective_state), worked out when reading and never written by a GET.
--
-- "Processing" (a summary being made) is not stored anywhere: it is derived from an active
-- summarize_job:<job id> task (ADR-098 item 8).

-- An employer. name is shown; name_key is what the domain reduces a name to, and being unique
-- it keeps "Larkspur Orchards" and "LARKSPUR ORCHARDS" from both existing. locations is a JSON
-- array of place names. A company that jobs point at can't be deleted.
CREATE TABLE companies (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL CHECK (trim(name, char(32, 9, 10, 13)) <> ''),
    name_key TEXT NOT NULL UNIQUE CHECK (trim(name_key, char(32, 9, 10, 13)) <> ''),
    website TEXT CHECK (
        website IS NULL OR website GLOB 'http://?*' OR website GLOB 'https://?*'
    ),
    industry TEXT,
    size TEXT,
    locations TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(locations) THEN json_type(locations) = 'array' ELSE 0 END
    ),
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
);

-- One job ad. snapshot_text is the cleaned job text as captured at captured_at (fetched or
-- pasted, M4.3); found_at is when the user added it and is the queue's sort.
--
-- The filter columns (industry to posted_date) hold the latest summary's values merged with
-- the user's corrections: user_corrected is a JSON array of the field names the user edited,
-- which a new summary never replaces (app.domain.jobs.merge_fields; AC-M4.9). Industry, work
-- mode, employment type and seniority hold a controlled_values value when the summary's value
-- maps to one; an unknown value is kept as it is and queued in field_value_queue. locations is
-- a JSON array of place names, as companies.locations (the domain checks they are strings).
-- Experience is in whole years; salary amounts may have cents (hourly rates), with currency and
-- period as free text the domain checks; a minimum is never above its maximum. fit_band and deal_breaker copy the latest assessment for
-- filtering; fit_band is NULL until one exists, and the band is computed in code, never taken
-- from the model (ADR-098 item 6).
--
-- Triage (app.domain.jobs): triage_state is one of new, seen, snoozed, removed, to_apply
-- (ADR-098 item 3: To apply until M5 creates applications). previous_state is the state Undo
-- goes back to, only ever new or seen. snooze_until is the local date a snooze ends.
-- removal_reason is the chip the user picked, if any, and removal_text their own words; both
-- are kept after a restore, so M9 can learn from removals. The states don't constrain each
-- other here; the domain's transitions do.
--
-- A near-duplicate merged into another job's card (M4.3) points at it with merged_into_job_id;
-- "Not a duplicate" clears it. A job can't be merged into itself.
CREATE TABLE jobs (
    id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT REFERENCES companies (id),
    title TEXT NOT NULL,
    snapshot_text TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    industry TEXT,
    department TEXT,
    work_mode TEXT,
    locations TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(locations) THEN json_type(locations) = 'array' ELSE 0 END
    ),
    employment_type TEXT,
    seniority TEXT,
    experience_min INTEGER CHECK (
        experience_min IS NULL OR (typeof(experience_min) = 'integer' AND experience_min >= 0)
    ),
    experience_max INTEGER CHECK (
        experience_max IS NULL OR (typeof(experience_max) = 'integer' AND experience_max >= 0)
    ),
    experience_text TEXT,
    salary_min NUMERIC CHECK (
        salary_min IS NULL OR (typeof(salary_min) IN ('integer', 'real') AND salary_min >= 0)
    ),
    salary_max NUMERIC CHECK (
        salary_max IS NULL OR (typeof(salary_max) IN ('integer', 'real') AND salary_max >= 0)
    ),
    salary_currency TEXT,
    salary_period TEXT,
    posted_date TEXT CHECK (
        posted_date IS NULL OR posted_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'
    ),
    fit_band TEXT CHECK (fit_band IS NULL OR fit_band IN ('strong', 'possible', 'stretch')),
    deal_breaker INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(deal_breaker) = 'integer' AND deal_breaker IN (0, 1)
    ),
    triage_state TEXT NOT NULL DEFAULT 'new' CHECK (
        triage_state IN ('new', 'seen', 'snoozed', 'removed', 'to_apply')
    ),
    previous_state TEXT CHECK (previous_state IS NULL OR previous_state IN ('new', 'seen')),
    snooze_until TEXT CHECK (
        snooze_until IS NULL OR snooze_until GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'
    ),
    removal_reason TEXT CHECK (
        removal_reason IS NULL OR removal_reason IN (
            'too_junior',
            'too_senior',
            'location',
            'salary',
            'industry',
            'company',
            'not_interested',
            'other'
        )
    ),
    removal_text TEXT,
    notes TEXT,
    user_corrected TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(user_corrected) THEN json_type(user_corrected) = 'array' ELSE 0 END
    ),
    merged_into_job_id TEXT REFERENCES jobs (id) CHECK (
        merged_into_job_id IS NULL OR merged_into_job_id <> id
    ),
    found_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (experience_min IS NULL OR experience_max IS NULL OR experience_min <= experience_max),
    CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max)
);

-- The queue reads one state newest first (M4.6); due snoozes are found by state and date.
CREATE INDEX jobs_triage_state_found_at ON jobs (triage_state, found_at);
CREATE INDEX jobs_triage_state_snooze_until ON jobs (triage_state, snooze_until);
-- Every other inbox filter, group and sort (M4.6), the company page, and the merged cards.
CREATE INDEX jobs_found_at ON jobs (found_at);
CREATE INDEX jobs_posted_date ON jobs (posted_date);
CREATE INDEX jobs_fit_band ON jobs (fit_band);
CREATE INDEX jobs_deal_breaker ON jobs (deal_breaker);
CREATE INDEX jobs_work_mode ON jobs (work_mode);
CREATE INDEX jobs_industry ON jobs (industry);
CREATE INDEX jobs_seniority ON jobs (seniority);
CREATE INDEX jobs_employment_type ON jobs (employment_type);
CREATE INDEX jobs_company_id ON jobs (company_id);
CREATE INDEX jobs_merged_into_job_id ON jobs (merged_into_job_id);

-- INSERT OR REPLACE removes the row it replaces without running delete triggers, which would
-- leave a second search entry for the job. A job changes only through UPDATE.
CREATE TRIGGER jobs_never_replaced
BEFORE INSERT ON jobs
WHEN EXISTS (SELECT 1 FROM jobs WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'jobs rows are never replaced');
END;

-- Where a job was found (M4.3). kind is 'fetched' (the page at url was fetched), 'pasted' (the
-- user pasted the text, with or without a link) or 'link' (the link to the original ad given
-- with pasted text, stored and never fetched). Only a pasted source may have no URL. url is the
-- address as given, or the final address after redirects for a fetched page; canonical_url is
-- its canonical form (fragment and tracking parameters dropped), which duplicates are found by,
-- and is there exactly when url is. The same canonical URL found again adds a source to the job
-- that has it, so it is indexed but not unique; a merged duplicate keeps its own sources ("also
-- posted on N sites"). Sources are only added, so there is no updated_at or version.
CREATE TABLE job_sources (
    id TEXT PRIMARY KEY NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs (id),
    url TEXT CHECK (url IS NULL OR url GLOB 'http://?*' OR url GLOB 'https://?*'),
    canonical_url TEXT CHECK (
        canonical_url IS NULL
        OR canonical_url GLOB 'http://?*'
        OR canonical_url GLOB 'https://?*'
    ),
    kind TEXT NOT NULL CHECK (kind IN ('fetched', 'pasted', 'link')),
    found_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (kind = 'pasted' OR url IS NOT NULL),
    CHECK ((url IS NULL) = (canonical_url IS NULL))
);

CREATE INDEX job_sources_job_id ON job_sources (job_id);
CREATE INDEX job_sources_canonical_url ON job_sources (canonical_url);

-- One summary of a job by the summarize_job task (M4.4): summary is the short plain text shown
-- on the card (escaped, never Markdown; ADR-098 item 2) and fields_json the extracted fields as
-- a JSON object, before the merge with the user's corrections. provider, model, prompt_id and
-- prompt_version record what made it, and task_id the task that ran. A new summary is a new
-- row; the latest by created_at is the current one (spec 5 M2 rule 3). Rows are never changed
-- or deleted, so there is no updated_at or version: the triggers below refuse UPDATE, DELETE
-- and an insert that would take the place of an existing row (INSERT OR REPLACE). For the same
-- reason task_id has no ON DELETE action: a task with results can't be deleted.
-- UNIQUE (job_id, id) is the parent key that ties an assessment to a summary of its own job.
CREATE TABLE job_summaries (
    id TEXT PRIMARY KEY NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs (id),
    summary TEXT NOT NULL,
    fields_json TEXT NOT NULL CHECK (
        CASE WHEN json_valid(fields_json) THEN json_type(fields_json) = 'object' ELSE 0 END
    ),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    task_id TEXT REFERENCES tasks (id),
    created_at TEXT NOT NULL,
    UNIQUE (job_id, id)
);

-- A job's summaries, latest first.
CREATE INDEX job_summaries_job_id_created_at ON job_summaries (job_id, created_at);

CREATE TRIGGER job_summaries_never_updated
BEFORE UPDATE ON job_summaries
BEGIN
    SELECT RAISE(ABORT, 'job_summaries rows are never changed');
END;

CREATE TRIGGER job_summaries_never_deleted
BEFORE DELETE ON job_summaries
BEGIN
    SELECT RAISE(ABORT, 'job_summaries rows are never deleted');
END;

CREATE TRIGGER job_summaries_never_replaced
BEFORE INSERT ON job_summaries
WHEN EXISTS (SELECT 1 FROM job_summaries WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'job_summaries rows are never replaced');
END;

-- A requirement a summary lists (M4.4), in the ad's order: kind is must_have, key or nice.
-- Part of its summary, so rows are only inserted, like the summary itself.
CREATE TABLE job_requirements (
    id TEXT PRIMARY KEY NOT NULL,
    summary_id TEXT NOT NULL REFERENCES job_summaries (id),
    position INTEGER NOT NULL CHECK (typeof(position) = 'integer' AND position >= 1),
    kind TEXT NOT NULL CHECK (kind IN ('must_have', 'key', 'nice')),
    text TEXT NOT NULL CHECK (trim(text, char(32, 9, 10, 13)) <> ''),
    UNIQUE (summary_id, position)
);

CREATE TRIGGER job_requirements_never_updated
BEFORE UPDATE ON job_requirements
BEGIN
    SELECT RAISE(ABORT, 'job_requirements rows are never changed');
END;

CREATE TRIGGER job_requirements_never_deleted
BEFORE DELETE ON job_requirements
BEGIN
    SELECT RAISE(ABORT, 'job_requirements rows are never deleted');
END;

CREATE TRIGGER job_requirements_never_replaced
BEFORE INSERT ON job_requirements
WHEN EXISTS (
    SELECT 1 FROM job_requirements
    WHERE id = NEW.id OR (summary_id = NEW.summary_id AND position = NEW.position)
)
BEGIN
    SELECT RAISE(ABORT, 'job_requirements rows are never replaced');
END;

-- One fit assessment of a job by the assess_fit task (M4.5), against one summary of that same
-- job. result_json is the checked result as a JSON object (requirements met and missing,
-- deal-breakers found); fit_band is computed from it in code and deal_breaker is 1 when any
-- deal-breaker was flagged (ADR-098 item 6); fact_refs_json is the JSON array of the fact
-- references (F12@3) sent in that request. Provenance and append-only rules as for
-- job_summaries.
CREATE TABLE job_assessments (
    id TEXT PRIMARY KEY NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs (id),
    summary_id TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK (
        CASE WHEN json_valid(result_json) THEN json_type(result_json) = 'object' ELSE 0 END
    ),
    fit_band TEXT NOT NULL CHECK (fit_band IN ('strong', 'possible', 'stretch')),
    deal_breaker INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(deal_breaker) = 'integer' AND deal_breaker IN (0, 1)
    ),
    fact_refs_json TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(fact_refs_json) THEN json_type(fact_refs_json) = 'array' ELSE 0 END
    ),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    task_id TEXT REFERENCES tasks (id),
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id, summary_id) REFERENCES job_summaries (job_id, id)
);

-- A job's assessments, latest first.
CREATE INDEX job_assessments_job_id_created_at ON job_assessments (job_id, created_at);
CREATE INDEX job_assessments_summary_id ON job_assessments (summary_id);

CREATE TRIGGER job_assessments_never_updated
BEFORE UPDATE ON job_assessments
BEGIN
    SELECT RAISE(ABORT, 'job_assessments rows are never changed');
END;

CREATE TRIGGER job_assessments_never_deleted
BEFORE DELETE ON job_assessments
BEGIN
    SELECT RAISE(ABORT, 'job_assessments rows are never deleted');
END;

CREATE TRIGGER job_assessments_never_replaced
BEFORE INSERT ON job_assessments
WHEN EXISTS (SELECT 1 FROM job_assessments WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'job_assessments rows are never replaced');
END;

-- The lists summaries are normalized to (M4.4): field is the jobs column (industry, work_mode,
-- employment_type, seniority), value what is stored in it, and aliases a JSON array of other
-- spellings that mean the same value (lower case). One row per field and value. The user can add
-- values from field_value_queue; the seed below is the starting list.
CREATE TABLE controlled_values (
    id TEXT PRIMARY KEY NOT NULL,
    field TEXT NOT NULL CHECK (trim(field, char(32, 9, 10, 13)) <> ''),
    value TEXT NOT NULL CHECK (trim(value, char(32, 9, 10, 13)) <> ''),
    aliases TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(aliases) THEN json_type(aliases) = 'array' ELSE 0 END
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    UNIQUE (field, value)
);

-- The starting lists. work_mode values are exactly the keys of app.domain.settings.WorkMode
-- (the job preferences' work modes); employment_type values are the ADR-064 keys (full_time,
-- part_time, contract, temporary, freelance, internship), which no preference field holds yet;
-- industry and seniority are a broad, generic start. Values are lower-case keys;
-- the interface turns them into labels. Seed rows carry this migration's date, not the
-- Clock's, so every database gets the same rows.
WITH seed (field, value, aliases) AS (
    VALUES
        ('work_mode', 'remote', '["fully remote", "work from home"]'),
        ('work_mode', 'hybrid', '["partly remote"]'),
        ('work_mode', 'on_site', '["onsite", "on-site", "on site", "in office", "office-based"]'),
        ('employment_type', 'full_time', '["full-time", "full time"]'),
        ('employment_type', 'part_time', '["part-time", "part time"]'),
        ('employment_type', 'contract', '["contractor", "fixed-term", "fixed term"]'),
        ('employment_type', 'temporary', '["temp", "seasonal"]'),
        ('employment_type', 'freelance', '["freelancer", "self-employed"]'),
        ('employment_type', 'internship', '["intern", "placement", "traineeship"]'),
        ('seniority', 'entry', '["entry level", "entry-level", "graduate"]'),
        ('seniority', 'junior', '["jr"]'),
        ('seniority', 'mid', '["mid-level", "mid level", "intermediate"]'),
        ('seniority', 'senior', '["sr"]'),
        ('seniority', 'lead', '["team lead", "staff", "principal"]'),
        ('seniority', 'manager', '["management", "head of team"]'),
        ('seniority', 'director', '["head of", "vice president", "vp"]'),
        ('seniority', 'executive', '["c-level", "chief"]'),
        ('industry', 'agriculture', '["farming", "horticulture"]'),
        ('industry', 'construction', '[]'),
        ('industry', 'education', '["teaching", "schools"]'),
        ('industry', 'energy', '["utilities"]'),
        ('industry', 'finance', '["banking", "insurance", "financial services"]'),
        ('industry', 'government', '["public sector"]'),
        ('industry', 'healthcare', '["health", "medical"]'),
        ('industry', 'hospitality', '["hotels", "restaurants", "tourism"]'),
        ('industry', 'logistics', '["supply chain", "warehousing"]'),
        ('industry', 'manufacturing', '["production"]'),
        ('industry', 'media', '["publishing", "entertainment"]'),
        ('industry', 'nonprofit', '["non-profit", "charity"]'),
        ('industry', 'professional_services', '["consulting", "legal", "accounting"]'),
        ('industry', 'real_estate', '["property"]'),
        ('industry', 'retail', '["e-commerce", "ecommerce"]'),
        ('industry', 'technology', '["software", "it", "tech"]'),
        ('industry', 'telecommunications', '["telecom", "telecoms"]'),
        ('industry', 'transport', '["transportation", "shipping", "aviation"]')
)
INSERT INTO controlled_values (id, field, value, aliases, created_at, updated_at)
SELECT
    'cv-' || field || '-' || value,
    field,
    value,
    aliases,
    '2026-10-19T00:00:00+00:00',
    '2026-10-19T00:00:00+00:00'
FROM seed;

-- A summary value that matched no controlled value or alias (M4.4), once per field and value,
-- for the user to map to a value (it becomes an alias), add as a new value, or ignore.
CREATE TABLE field_value_queue (
    id TEXT PRIMARY KEY NOT NULL,
    field TEXT NOT NULL CHECK (trim(field, char(32, 9, 10, 13)) <> ''),
    value TEXT NOT NULL CHECK (trim(value, char(32, 9, 10, 13)) <> ''),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'mapped', 'ignored')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    UNIQUE (field, value)
);

-- The values still waiting.
CREATE INDEX field_value_queue_status ON field_value_queue (status);

-- Pairs of jobs the user said are not duplicates ("Not a duplicate", M4.3), so they are never
-- merged again. Each pair is stored once, with the smaller id first. Rows are only inserted, so
-- there is no updated_at or version.
CREATE TABLE job_pair_dismissals (
    job_a_id TEXT NOT NULL REFERENCES jobs (id),
    job_b_id TEXT NOT NULL REFERENCES jobs (id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_a_id, job_b_id),
    CHECK (job_a_id < job_b_id)
);

-- The second job of a pair, for lookups by either side.
CREATE INDEX job_pair_dismissals_job_b_id ON job_pair_dismissals (job_b_id);

-- A named inbox view (M4.6): filters_json is a JSON object of the filters, grouping and sort,
-- checked by the domain when read. Names are unique ignoring letter case; SQLite's NOCASE folds
-- only A to Z, so the domain compares names casefolded before saving.
CREATE TABLE saved_views (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (trim(name, char(32, 9, 10, 13)) <> ''),
    filters_json TEXT NOT NULL CHECK (
        CASE WHEN json_valid(filters_json) THEN json_type(filters_json) = 'object' ELSE 0 END
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
);

-- Full-text search over jobs (M4.6): one entry per job, holding its title, its company's name,
-- its latest summary and its snapshot. job_id is stored for joins and isn't searchable. Job ids
-- are text, and a text-keyed table's rowid can change on VACUUM, so entries are found by job_id;
-- that scans the index's rows, which is fine at a personal inbox's size. Search text from the
-- user must be passed as quoted tokens (M4.6).
CREATE VIRTUAL TABLE jobs_fts USING fts5 (
    job_id UNINDEXED,
    title,
    company,
    summary,
    snapshot
);

-- What each job's search entry should hold right now. The latest summary is the newest by
-- created_at (rowid breaks a tie: the one written last).
CREATE VIEW jobs_fts_source AS
SELECT
    j.id AS job_id,
    j.title AS title,
    (SELECT c.name FROM companies AS c WHERE c.id = j.company_id) AS company,
    (
        SELECT s.summary
        FROM job_summaries AS s
        WHERE s.job_id = j.id
        ORDER BY s.created_at DESC, s.rowid DESC
        LIMIT 1
    ) AS summary,
    j.snapshot_text AS snapshot
FROM jobs AS j;

-- Every trigger below does the same two steps for the jobs a change touches: delete their
-- entries, then insert what jobs_fts_source holds for them now.

CREATE TRIGGER jobs_fts_after_job_insert
AFTER INSERT ON jobs
BEGIN
    DELETE FROM jobs_fts WHERE job_id = NEW.id;
    INSERT INTO jobs_fts (job_id, title, company, summary, snapshot)
    SELECT job_id, title, company, summary, snapshot FROM jobs_fts_source WHERE job_id = NEW.id;
END;

-- A new title or snapshot, or a move to another company or to none. Other changes (triage,
-- filters) leave the entry as it is.
CREATE TRIGGER jobs_fts_after_job_update
AFTER UPDATE OF id, title, snapshot_text, company_id ON jobs
WHEN NEW.id IS NOT OLD.id
    OR NEW.title IS NOT OLD.title
    OR NEW.snapshot_text IS NOT OLD.snapshot_text
    OR NEW.company_id IS NOT OLD.company_id
BEGIN
    DELETE FROM jobs_fts WHERE job_id IN (OLD.id, NEW.id);
    INSERT INTO jobs_fts (job_id, title, company, summary, snapshot)
    SELECT job_id, title, company, summary, snapshot FROM jobs_fts_source WHERE job_id = NEW.id;
END;

CREATE TRIGGER jobs_fts_after_job_delete
AFTER DELETE ON jobs
BEGIN
    DELETE FROM jobs_fts WHERE job_id = OLD.id;
END;

-- Renaming a company reaches every job at it.
CREATE TRIGGER jobs_fts_after_company_update
AFTER UPDATE OF name ON companies
WHEN NEW.name IS NOT OLD.name
BEGIN
    DELETE FROM jobs_fts WHERE job_id IN (SELECT id FROM jobs WHERE company_id = NEW.id);
    INSERT INTO jobs_fts (job_id, title, company, summary, snapshot)
    SELECT job_id, title, company, summary, snapshot
    FROM jobs_fts_source
    WHERE job_id IN (SELECT id FROM jobs WHERE company_id = NEW.id);
END;

-- A new summary may be the latest; summaries are never changed or deleted.
CREATE TRIGGER jobs_fts_after_summary_insert
AFTER INSERT ON job_summaries
BEGIN
    DELETE FROM jobs_fts WHERE job_id = NEW.job_id;
    INSERT INTO jobs_fts (job_id, title, company, summary, snapshot)
    SELECT job_id, title, company, summary, snapshot
    FROM jobs_fts_source
    WHERE job_id = NEW.job_id;
END;
