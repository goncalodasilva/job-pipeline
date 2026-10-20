-- M5 applications, contacts, events and reminders (ADR-102, ADR-103).
-- UTC instants use ISO 8601; reminder snoozes are local calendar dates.
-- History is immutable for retained applications. The guarded Undo exception
-- removes only an untouched application and its sole creation/backfill row.

CREATE TABLE application_statuses (
    key TEXT PRIMARY KEY NOT NULL CHECK (trim(key) <> ''),
    label TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (trim(label, char(32, 9, 10, 13)) <> ''),
    phase TEXT NOT NULL CHECK (phase IN ('preparing', 'active', 'closed')),
    position INTEGER NOT NULL CHECK (typeof(position) = 'integer' AND position >= 0),
    built_in INTEGER NOT NULL DEFAULT 0 CHECK (typeof(built_in) = 'integer' AND built_in IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
) WITHOUT ROWID;
CREATE INDEX application_statuses_phase_position ON application_statuses (phase, position, key);
CREATE TRIGGER application_statuses_builtin_never_deleted
BEFORE DELETE ON application_statuses WHEN OLD.built_in = 1
BEGIN
    SELECT RAISE(ABORT, 'built-in statuses cannot be deleted');
END;
CREATE TRIGGER application_statuses_never_replaced
BEFORE INSERT ON application_statuses
WHEN EXISTS (SELECT 1 FROM application_statuses WHERE key = NEW.key OR label = NEW.label COLLATE NOCASE)
BEGIN
    SELECT RAISE(ABORT, 'statuses are changed by update only');
END;
-- UPDATE OR REPLACE can also delete a conflicting row without running DELETE
-- triggers. Reject collisions before conflict handling and keep stable identities.
CREATE TRIGGER application_statuses_identity_kept
BEFORE UPDATE OF key, built_in ON application_statuses
WHEN NEW.key IS NOT OLD.key OR NEW.built_in IS NOT OLD.built_in
BEGIN
    SELECT RAISE(ABORT, 'status identities are kept');
END;
CREATE TRIGGER application_statuses_label_unique_on_update
BEFORE UPDATE OF label ON application_statuses
WHEN EXISTS (
    SELECT 1 FROM application_statuses WHERE key <> OLD.key AND label = NEW.label COLLATE NOCASE
)
BEGIN
    SELECT RAISE(ABORT, 'status labels must be unique');
END;

CREATE TABLE applications (
    id TEXT PRIMARY KEY NOT NULL,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs (id),
    status_key TEXT NOT NULL REFERENCES application_statuses (key),
    applied_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
) WITHOUT ROWID;
CREATE INDEX applications_status_activity ON applications (status_key, updated_at, id);
CREATE INDEX applications_applied_at ON applications (applied_at, id);
CREATE INDEX applications_updated_at ON applications (updated_at, id);
CREATE INDEX jobs_title_application_sort ON jobs (title COLLATE NOCASE, id);
CREATE INDEX companies_name_application_sort ON companies (name COLLATE NOCASE, id);
CREATE TRIGGER jobs_identity_kept_for_applications
BEFORE UPDATE OF id ON jobs WHEN NEW.id IS NOT OLD.id
BEGIN
    SELECT RAISE(ABORT, 'job identities are kept');
END;
CREATE TRIGGER applications_never_replaced
BEFORE INSERT ON applications
WHEN EXISTS (SELECT 1 FROM applications WHERE id = NEW.id OR job_id = NEW.job_id)
BEGIN
    SELECT RAISE(ABORT, 'applications are changed by update only');
END;
CREATE TRIGGER applications_identity_kept
BEFORE UPDATE OF id, job_id ON applications
WHEN NEW.id IS NOT OLD.id OR NEW.job_id IS NOT OLD.job_id
BEGIN
    SELECT RAISE(ABORT, 'application identities are kept');
END;
CREATE TRIGGER applications_first_applied_time_kept
BEFORE UPDATE OF applied_at ON applications
WHEN OLD.applied_at IS NOT NULL AND NEW.applied_at IS NOT OLD.applied_at
BEGIN
    SELECT RAISE(ABORT, 'the first applied time is kept');
END;

CREATE TABLE status_history (
    id TEXT PRIMARY KEY NOT NULL,
    application_id TEXT NOT NULL REFERENCES applications (id) ON DELETE CASCADE,
    from_status TEXT REFERENCES application_statuses (key),
    to_status TEXT NOT NULL REFERENCES application_statuses (key),
    changed_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('created', 'board', 'menu', 'drawer', 'backfill')),
    created_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX status_history_application_changed ON status_history (application_id, changed_at, id);
CREATE INDEX status_history_to_status_changed ON status_history (to_status, changed_at);
CREATE INDEX status_history_from_status ON status_history (from_status);
CREATE TRIGGER status_history_never_updated
BEFORE UPDATE ON status_history
BEGIN
    SELECT RAISE(ABORT, 'history rows cannot be changed');
END;
-- SQLite removes the parent before applying foreign-key actions. The application
-- delete guard checks eligibility first; direct child deletion is always refused.
CREATE TRIGGER status_history_never_deleted_directly
BEFORE DELETE ON status_history
WHEN EXISTS (SELECT 1 FROM applications WHERE id = OLD.application_id)
BEGIN
    SELECT RAISE(ABORT, 'history rows cannot be deleted directly');
END;
CREATE TRIGGER status_history_never_replaced
BEFORE INSERT ON status_history WHEN EXISTS (SELECT 1 FROM status_history WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'history rows cannot be replaced');
END;

CREATE TABLE application_notes (
    id TEXT PRIMARY KEY NOT NULL,
    application_id TEXT NOT NULL REFERENCES applications (id),
    kind TEXT NOT NULL CHECK (kind IN ('note', 'feedback')),
    text TEXT NOT NULL CHECK (trim(text, char(32, 9, 10, 13)) <> ''),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
);
CREATE INDEX application_notes_application_created ON application_notes (application_id, created_at, id);

CREATE TABLE contacts (
    id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES companies (id),
    name TEXT NOT NULL CHECK (trim(name, char(32, 9, 10, 13)) <> ''),
    role TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    relationship TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
) WITHOUT ROWID;
CREATE INDEX contacts_company_name ON contacts (company_id, name COLLATE NOCASE, id);
CREATE TRIGGER contacts_never_replaced
BEFORE INSERT ON contacts WHEN EXISTS (SELECT 1 FROM contacts WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'contacts are changed by update only');
END;
CREATE TRIGGER contacts_identity_kept
BEFORE UPDATE OF id ON contacts WHEN NEW.id IS NOT OLD.id
BEGIN
    SELECT RAISE(ABORT, 'contact identities are kept');
END;
CREATE TABLE application_contacts (
    application_id TEXT NOT NULL REFERENCES applications (id),
    contact_id TEXT NOT NULL REFERENCES contacts (id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    PRIMARY KEY (application_id, contact_id)
);
CREATE INDEX application_contacts_contact ON application_contacts (contact_id, application_id);
CREATE TRIGGER application_contacts_same_company_insert
BEFORE INSERT ON application_contacts
WHEN NOT EXISTS (
    SELECT 1 FROM applications a JOIN jobs j ON j.id = a.job_id
    JOIN contacts c ON c.company_id = j.company_id
    WHERE a.id = NEW.application_id AND c.id = NEW.contact_id
)
BEGIN
    SELECT RAISE(ABORT, 'contact must belong to the application company');
END;
CREATE TRIGGER application_contacts_same_company_update
BEFORE UPDATE OF application_id, contact_id ON application_contacts
WHEN NOT EXISTS (
    SELECT 1 FROM applications a JOIN jobs j ON j.id = a.job_id
    JOIN contacts c ON c.company_id = j.company_id
    WHERE a.id = NEW.application_id AND c.id = NEW.contact_id
)
BEGIN
    SELECT RAISE(ABORT, 'contact must belong to the application company');
END;
CREATE TRIGGER contacts_company_keeps_application_links
BEFORE UPDATE OF company_id ON contacts
WHEN EXISTS (
    SELECT 1 FROM application_contact_links ac JOIN applications a ON a.id = ac.application_id
    JOIN jobs j ON j.id = a.job_id
    WHERE ac.contact_id = OLD.id AND j.company_id IS NOT NEW.company_id
)
BEGIN
    SELECT RAISE(ABORT, 'linked contacts keep their application company');
END;

CREATE TABLE interactions (
    id TEXT PRIMARY KEY NOT NULL,
    contact_id TEXT REFERENCES contacts (id),
    application_id TEXT REFERENCES applications (id),
    happened_at TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('email', 'call', 'video', 'in_person', 'message', 'other')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (contact_id IS NOT NULL OR application_id IS NOT NULL)
);
CREATE INDEX interactions_contact_happened ON interactions (contact_id, happened_at, id);
CREATE INDEX interactions_application_happened ON interactions (application_id, happened_at, id);

-- Both explicit links and logged interactions bind a contact to an application's
-- company. Parent edits must preserve that relationship too.
CREATE VIEW application_contact_links AS
SELECT application_id, contact_id FROM application_contacts
UNION
SELECT application_id, contact_id FROM interactions
WHERE application_id IS NOT NULL AND contact_id IS NOT NULL;
CREATE TRIGGER interactions_same_company_insert
BEFORE INSERT ON interactions
WHEN NEW.application_id IS NOT NULL AND NEW.contact_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM applications a JOIN jobs j ON j.id = a.job_id
    JOIN contacts c ON c.company_id = j.company_id
    WHERE a.id = NEW.application_id AND c.id = NEW.contact_id
)
BEGIN
    SELECT RAISE(ABORT, 'interaction contact must belong to the application company');
END;
CREATE TRIGGER interactions_same_company_update
BEFORE UPDATE OF application_id, contact_id ON interactions
WHEN NEW.application_id IS NOT NULL AND NEW.contact_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM applications a JOIN jobs j ON j.id = a.job_id
    JOIN contacts c ON c.company_id = j.company_id
    WHERE a.id = NEW.application_id AND c.id = NEW.contact_id
)
BEGIN
    SELECT RAISE(ABORT, 'interaction contact must belong to the application company');
END;
CREATE TRIGGER jobs_company_keeps_contact_links
BEFORE UPDATE OF company_id ON jobs
WHEN EXISTS (
    SELECT 1 FROM applications a JOIN application_contact_links ac ON ac.application_id = a.id
    JOIN contacts c ON c.id = ac.contact_id
    WHERE a.job_id = OLD.id AND c.company_id IS NOT NEW.company_id
)
BEGIN
    SELECT RAISE(ABORT, 'application contacts keep their company');
END;

CREATE TABLE events (
    id TEXT PRIMARY KEY NOT NULL,
    application_id TEXT NOT NULL REFERENCES applications (id),
    type TEXT NOT NULL CHECK (type IN ('interview', 'follow_up', 'deadline', 'offer_deadline')),
    starts_at TEXT NOT NULL CHECK (
        julianday(starts_at) IS NOT NULL
        AND (substr(starts_at, -6) = '+00:00' OR substr(starts_at, -1) = 'Z')
    ),
    ends_at TEXT NOT NULL CHECK (
        julianday(ends_at) IS NOT NULL
        AND (substr(ends_at, -6) = '+00:00' OR substr(ends_at, -1) = 'Z')
        AND julianday(ends_at) >= julianday(starts_at)
    ),
    all_day INTEGER NOT NULL DEFAULT 0 CHECK (typeof(all_day) = 'integer' AND all_day IN (0, 1)),
    time_zone TEXT NOT NULL CHECK (trim(time_zone, char(32, 9, 10, 13)) <> ''),
    round TEXT NOT NULL DEFAULT '',
    format TEXT NOT NULL DEFAULT '',
    interviewers TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '',
    thank_you_reminder INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(thank_you_reminder) = 'integer' AND thank_you_reminder IN (0, 1)
    ),
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (all_day = 0 OR type IN ('deadline', 'offer_deadline'))
);
CREATE INDEX events_application_start ON events (application_id, status, starts_at, id);
CREATE INDEX events_status_start ON events (status, starts_at, id);
CREATE INDEX events_status_end_thank_you ON events (status, ends_at, thank_you_reminder);

CREATE TABLE reminder_rules (
    id TEXT PRIMARY KEY NOT NULL,
    status_key TEXT NOT NULL UNIQUE REFERENCES application_statuses (key),
    days INTEGER NOT NULL CHECK (typeof(days) = 'integer' AND days BETWEEN 1 AND 365),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (typeof(enabled) = 'integer' AND enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
);
CREATE INDEX reminder_rules_enabled_status ON reminder_rules (enabled, status_key);
CREATE TABLE reminder_states (
    id TEXT PRIMARY KEY NOT NULL,
    occurrence_key TEXT NOT NULL UNIQUE CHECK (trim(occurrence_key) <> ''),
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'done', 'dismissed', 'snoozed')),
    snooze_until TEXT CHECK (
        snooze_until IS NULL OR snooze_until GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'
    ),
    seen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK ((state = 'snoozed') = (snooze_until IS NOT NULL))
);
CREATE INDEX reminder_states_state_snooze ON reminder_states (state, snooze_until, occurrence_key);
CREATE INDEX reminder_states_seen ON reminder_states (seen_at, occurrence_key);

CREATE TRIGGER applications_delete_only_untouched
BEFORE DELETE ON applications
WHEN OLD.version <> 1 OR OLD.applied_at IS NOT NULL
    OR (SELECT count(*) FROM status_history WHERE application_id = OLD.id) <> 1
    OR NOT EXISTS (
        SELECT 1 FROM status_history h JOIN application_statuses s ON s.key = h.to_status
        WHERE h.application_id = OLD.id AND h.from_status IS NULL
            AND h.to_status = OLD.status_key AND s.phase = 'preparing'
            AND h.source IN ('created', 'backfill')
    )
    OR EXISTS (SELECT 1 FROM application_notes WHERE application_id = OLD.id)
    OR EXISTS (SELECT 1 FROM events WHERE application_id = OLD.id)
    OR EXISTS (SELECT 1 FROM application_contacts WHERE application_id = OLD.id)
    OR EXISTS (SELECT 1 FROM interactions WHERE application_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'this application already has changes');
END;

WITH seed (key, label, phase, position) AS (
    VALUES ('preparing', 'Preparing', 'preparing', 1),
        ('applied', 'Applied', 'active', 2),
        ('screening', 'Screening', 'active', 3),
        ('interviewing', 'Interviewing', 'active', 4),
        ('offer', 'Offer', 'active', 5),
        ('rejected', 'Rejected', 'closed', 6),
        ('withdrawn', 'Withdrawn', 'closed', 7),
        ('accepted', 'Accepted', 'closed', 8),
        ('declined', 'Declined', 'closed', 9)
)
INSERT INTO application_statuses (key, label, phase, position, built_in, created_at, updated_at)
SELECT key, label, phase, position, 1, '2026-10-20T00:00:00+00:00', '2026-10-20T00:00:00+00:00'
FROM seed;
INSERT INTO reminder_rules (id, status_key, days, enabled, created_at, updated_at)
VALUES ('applied-14-days', 'applied', 14, 1, '2026-10-20T00:00:00+00:00', '2026-10-20T00:00:00+00:00');

-- SQL-only backfill: safe ids, existing job timestamps, no wall-clock reads.
INSERT INTO applications (id, job_id, status_key, applied_at, created_at, updated_at)
SELECT lower(hex(randomblob(16))), id, 'preparing', NULL, updated_at, updated_at
FROM jobs WHERE triage_state = 'to_apply' AND merged_into_job_id IS NULL;
INSERT INTO status_history (id, application_id, from_status, to_status, changed_at, source, created_at)
SELECT lower(hex(randomblob(16))), id, NULL, status_key, created_at, 'backfill', created_at
FROM applications;
