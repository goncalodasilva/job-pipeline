-- M6 tailoring: workspaces, gap checks, angles, CV templates, documents and their versions,
-- units, citations, exports and the freeze at submission (ADR-090, ADR-104, ADR-106).
-- Personal details are a versioned setting (ADR-106 item 3), so there is no table for them.
--
-- Mutable tables (angles, workspaces, cv_templates, documents, document_versions) have
-- created_at, updated_at and version, and are changed by UPDATE only. Each refuses an insert that
-- would replace an existing row (INSERT OR REPLACE, REPLACE) and the updates that would collide
-- through UPDATE OR REPLACE. They are WITHOUT ROWID too: in a rowid table,
-- UPDATE OR REPLACE ... SET rowid = (another row's rowid) deletes that other row and fires no
-- trigger, and no foreign key stops it for a row nothing refers to. The other tables are history:
-- explicit key, WITHOUT ROWID, and triggers that refuse UPDATE, DELETE and an insert that would
-- take the place of a row. Each row's task_id has no ON DELETE action, so a task with results
-- can't be deleted.
-- Every citation names a real version of that fact (composite key to fact_versions).
-- A version starts as a draft and gains units, citations and sent facts only while it is a draft.

-- A named way to present the person for a job (M6.5): proposed by the AI or typed by the user.
CREATE TABLE angles (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL CHECK (trim(name, char(32, 9, 10, 13)) <> ''),
    description TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL CHECK (origin IN ('ai', 'user')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
) WITHOUT ROWID;

CREATE TRIGGER angles_never_replaced
BEFORE INSERT ON angles
WHEN EXISTS (SELECT 1 FROM angles WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'angles are changed by update only');
END;

CREATE TRIGGER angles_identity_kept
BEFORE UPDATE OF id ON angles
WHEN NEW.id IS NOT OLD.id
BEGIN
    SELECT RAISE(ABORT, 'angle identities are kept');
END;

-- One workspace per application: the CV country and angle chosen for it, and the letter's tone
-- and length.
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY NOT NULL,
    application_id TEXT NOT NULL UNIQUE REFERENCES applications (id),
    cv_country TEXT CHECK (cv_country IS NULL OR trim(cv_country, char(32, 9, 10, 13)) <> ''),
    angle_id TEXT REFERENCES angles (id),
    letter_tone TEXT CHECK (letter_tone IS NULL OR letter_tone IN ('warm', 'formal', 'direct')),
    letter_length TEXT CHECK (letter_length IS NULL OR letter_length IN ('short', 'medium', 'long')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1)
) WITHOUT ROWID;

CREATE INDEX workspaces_angle_id ON workspaces (angle_id);

CREATE TRIGGER workspaces_never_replaced
BEFORE INSERT ON workspaces
WHEN EXISTS (
    SELECT 1 FROM workspaces WHERE id = NEW.id OR application_id = NEW.application_id
)
BEGIN
    SELECT RAISE(ABORT, 'workspaces are changed by update only');
END;

CREATE TRIGGER workspaces_identity_kept
BEFORE UPDATE OF id, application_id ON workspaces
WHEN NEW.id IS NOT OLD.id OR NEW.application_id IS NOT OLD.application_id
BEGIN
    SELECT RAISE(ABORT, 'workspace identities are kept');
END;

-- One gap check per request (M6.5): the summary shown, the checked result as a JSON object and
-- the provenance. gap_check_facts lists the fact versions that request sent.
CREATE TABLE gap_checks (
    id TEXT PRIMARY KEY NOT NULL,
    application_id TEXT NOT NULL REFERENCES applications (id),
    summary TEXT NOT NULL,
    result TEXT NOT NULL CHECK (
        CASE WHEN json_valid(result) THEN json_type(result) = 'object' ELSE 0 END
    ),
    provider TEXT,
    model TEXT,
    prompt_id TEXT,
    prompt_version TEXT,
    task_id TEXT REFERENCES tasks (id),
    created_at TEXT NOT NULL
) WITHOUT ROWID;

CREATE INDEX gap_checks_application_id_created_at ON gap_checks (application_id, created_at);

CREATE TRIGGER gap_checks_never_updated
BEFORE UPDATE ON gap_checks
BEGIN
    SELECT RAISE(ABORT, 'gap_checks rows are never changed');
END;

CREATE TRIGGER gap_checks_never_deleted
BEFORE DELETE ON gap_checks
BEGIN
    SELECT RAISE(ABORT, 'gap_checks rows are never deleted');
END;

CREATE TRIGGER gap_checks_never_replaced
BEFORE INSERT ON gap_checks
WHEN EXISTS (SELECT 1 FROM gap_checks WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'gap_checks rows are never replaced');
END;

CREATE TABLE gap_check_facts (
    gap_check_id TEXT NOT NULL REFERENCES gap_checks (id),
    fact_id TEXT NOT NULL,
    fact_version_id TEXT NOT NULL,
    PRIMARY KEY (gap_check_id, fact_id),
    FOREIGN KEY (fact_id, fact_version_id) REFERENCES fact_versions (fact_id, id)
) WITHOUT ROWID;

CREATE INDEX gap_check_facts_fact_id ON gap_check_facts (fact_id);

CREATE TRIGGER gap_check_facts_never_updated
BEFORE UPDATE ON gap_check_facts
BEGIN
    SELECT RAISE(ABORT, 'gap_check_facts rows are never changed');
END;

CREATE TRIGGER gap_check_facts_never_deleted
BEFORE DELETE ON gap_check_facts
BEGIN
    SELECT RAISE(ABORT, 'gap_check_facts rows are never deleted');
END;

CREATE TRIGGER gap_check_facts_never_replaced
BEFORE INSERT ON gap_check_facts
WHEN EXISTS (
    SELECT 1 FROM gap_check_facts
    WHERE gap_check_id = NEW.gap_check_id AND fact_id = NEW.fact_id
)
BEGIN
    SELECT RAISE(ABORT, 'gap_check_facts rows are never replaced');
END;

-- One request for angle proposals (M6.5), kept as history; result is the checked JSON object.
CREATE TABLE angle_proposals (
    id TEXT PRIMARY KEY NOT NULL,
    application_id TEXT NOT NULL REFERENCES applications (id),
    result TEXT CHECK (
        result IS NULL OR CASE WHEN json_valid(result) THEN json_type(result) = 'object' ELSE 0 END
    ),
    provider TEXT,
    model TEXT,
    prompt_id TEXT,
    prompt_version TEXT,
    task_id TEXT REFERENCES tasks (id),
    created_at TEXT NOT NULL
) WITHOUT ROWID;

CREATE INDEX angle_proposals_application_id_created_at
ON angle_proposals (application_id, created_at);

CREATE TRIGGER angle_proposals_never_updated
BEFORE UPDATE ON angle_proposals
BEGIN
    SELECT RAISE(ABORT, 'angle_proposals rows are never changed');
END;

CREATE TRIGGER angle_proposals_never_deleted
BEFORE DELETE ON angle_proposals
BEGIN
    SELECT RAISE(ABORT, 'angle_proposals rows are never deleted');
END;

CREATE TRIGGER angle_proposals_never_replaced
BEFORE INSERT ON angle_proposals
WHEN EXISTS (SELECT 1 FROM angle_proposals WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'angle_proposals rows are never replaced');
END;

-- One CV template per country: a closed spec as a JSON object, drafted by the AI and approved by
-- the user. An approved template has an approved_at time and a draft has none. Editing an approved
-- template is an UPDATE back to draft (approved_at cleared), so it needs approving again.
CREATE TABLE cv_templates (
    id TEXT PRIMARY KEY NOT NULL,
    country TEXT NOT NULL UNIQUE CHECK (trim(country, char(32, 9, 10, 13)) <> ''),
    status TEXT NOT NULL CHECK (status IN ('draft', 'approved')),
    spec TEXT NOT NULL CHECK (
        CASE WHEN json_valid(spec) THEN json_type(spec) = 'object' ELSE 0 END
    ),
    approved_at TEXT,
    provider TEXT,
    model TEXT,
    prompt_id TEXT,
    prompt_version TEXT,
    task_id TEXT REFERENCES tasks (id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK ((status = 'approved') = (approved_at IS NOT NULL))
) WITHOUT ROWID;

CREATE TRIGGER cv_templates_never_replaced
BEFORE INSERT ON cv_templates
WHEN EXISTS (SELECT 1 FROM cv_templates WHERE id = NEW.id OR country = NEW.country)
BEGIN
    SELECT RAISE(ABORT, 'cv_templates are changed by update only');
END;

CREATE TRIGGER cv_templates_identity_kept
BEFORE UPDATE OF id, country ON cv_templates
WHEN NEW.id IS NOT OLD.id OR NEW.country IS NOT OLD.country
BEGIN
    SELECT RAISE(ABORT, 'template identities are kept');
END;

-- One document per application and kind. current_version_id always names a version of this same
-- document. The key is deferred because the document is written first and its first version
-- later in the same transaction (as facts.current_version_id); the commit checks it.
CREATE TABLE documents (
    id TEXT PRIMARY KEY NOT NULL,
    application_id TEXT NOT NULL REFERENCES applications (id),
    kind TEXT NOT NULL CHECK (kind IN ('cv', 'cover_letter')),
    current_version_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    UNIQUE (application_id, kind),
    FOREIGN KEY (id, current_version_id) REFERENCES document_versions (document_id, id)
        DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;

CREATE TRIGGER documents_never_replaced
BEFORE INSERT ON documents
WHEN EXISTS (
    SELECT 1 FROM documents
    WHERE id = NEW.id OR (application_id = NEW.application_id AND kind = NEW.kind)
)
BEGIN
    SELECT RAISE(ABORT, 'documents are changed by update only');
END;

CREATE TRIGGER documents_identity_kept
BEFORE UPDATE OF id, application_id, kind ON documents
WHEN NEW.id IS NOT OLD.id OR NEW.application_id IS NOT OLD.application_id
    OR NEW.kind IS NOT OLD.kind
BEGIN
    SELECT RAISE(ABORT, 'document identities are kept');
END;

-- A version of a document. number counts the document's versions; version is the row counter.
-- Everything but status and the row counters is fixed at insert. status moves forward only,
-- draft to approved to sent, and a version is never deleted.
-- UNIQUE (document_id, id) is the parent key of documents.current_version_id and of
-- parent_version_id, which stays inside the same document.
CREATE TABLE document_versions (
    id TEXT PRIMARY KEY NOT NULL,
    document_id TEXT NOT NULL REFERENCES documents (id),
    number INTEGER NOT NULL CHECK (typeof(number) = 'integer' AND number >= 1),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'sent')),
    origin TEXT NOT NULL CHECK (trim(origin, char(32, 9, 10, 13)) <> ''),
    parent_version_id TEXT,
    template_snapshot TEXT CHECK (
        template_snapshot IS NULL
        OR CASE WHEN json_valid(template_snapshot) THEN json_type(template_snapshot) = 'object'
            ELSE 0 END
    ),
    angle_text TEXT,
    tone TEXT CHECK (tone IS NULL OR tone IN ('warm', 'formal', 'direct')),
    length TEXT CHECK (length IS NULL OR length IN ('short', 'medium', 'long')),
    note TEXT,
    provider TEXT,
    model TEXT,
    prompt_id TEXT,
    prompt_version TEXT,
    task_id TEXT REFERENCES tasks (id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version) = 'integer' AND version >= 1),
    UNIQUE (document_id, number),
    UNIQUE (document_id, id),
    FOREIGN KEY (document_id, parent_version_id) REFERENCES document_versions (document_id, id)
) WITHOUT ROWID;

CREATE INDEX document_versions_parent ON document_versions (parent_version_id);

CREATE TRIGGER document_versions_never_deleted
BEFORE DELETE ON document_versions
BEGIN
    SELECT RAISE(ABORT, 'document_versions rows are never deleted');
END;

CREATE TRIGGER document_versions_never_replaced
BEFORE INSERT ON document_versions
WHEN EXISTS (
    SELECT 1 FROM document_versions
    WHERE id = NEW.id OR (document_id = NEW.document_id AND number = NEW.number)
)
BEGIN
    SELECT RAISE(ABORT, 'document_versions rows are never replaced');
END;

-- A version starts as a draft; approving and sending are updates (below).
CREATE TRIGGER document_versions_start_as_draft
BEFORE INSERT ON document_versions
WHEN NEW.status IS NOT 'draft'
BEGIN
    SELECT RAISE(ABORT, 'a document version starts as a draft');
END;

CREATE TRIGGER document_versions_content_kept
BEFORE UPDATE OF id, document_id, number, origin, parent_version_id, template_snapshot,
    angle_text, tone, length, note, provider, model, prompt_id, prompt_version, task_id,
    created_at ON document_versions
WHEN NEW.id IS NOT OLD.id OR NEW.document_id IS NOT OLD.document_id
    OR NEW.number IS NOT OLD.number OR NEW.origin IS NOT OLD.origin
    OR NEW.parent_version_id IS NOT OLD.parent_version_id
    OR NEW.template_snapshot IS NOT OLD.template_snapshot
    OR NEW.angle_text IS NOT OLD.angle_text OR NEW.tone IS NOT OLD.tone
    OR NEW.length IS NOT OLD.length OR NEW.note IS NOT OLD.note
    OR NEW.provider IS NOT OLD.provider OR NEW.model IS NOT OLD.model
    OR NEW.prompt_id IS NOT OLD.prompt_id OR NEW.prompt_version IS NOT OLD.prompt_version
    OR NEW.task_id IS NOT OLD.task_id OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'document version content is never changed');
END;

CREATE TRIGGER document_versions_status_forward_only
BEFORE UPDATE OF status ON document_versions
WHEN NEW.status IS NOT OLD.status
    AND NOT ((OLD.status = 'draft' AND NEW.status = 'approved')
        OR (OLD.status = 'approved' AND NEW.status = 'sent'))
BEGIN
    SELECT RAISE(ABORT, 'document version status moves forward only');
END;

-- The lines of one document version, in order. removed is a tombstone: the user removed that
-- line, and the record keeps it so a regeneration doesn't bring it back. kind is structure
-- (a heading) or content; added_details and flags are JSON arrays.
CREATE TABLE document_units (
    id TEXT PRIMARY KEY NOT NULL,
    document_version_id TEXT NOT NULL REFERENCES document_versions (id),
    position INTEGER NOT NULL CHECK (typeof(position) = 'integer' AND position >= 0),
    section TEXT NOT NULL,
    role_id TEXT REFERENCES roles (id),
    kind TEXT NOT NULL CHECK (kind IN ('structure', 'content')),
    text TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('ai', 'user')),
    removed INTEGER NOT NULL DEFAULT 0 CHECK (typeof(removed) = 'integer' AND removed IN (0, 1)),
    added_details TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(added_details) THEN json_type(added_details) = 'array' ELSE 0 END
    ),
    flags TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(flags) THEN json_type(flags) = 'array' ELSE 0 END
    ),
    created_at TEXT NOT NULL,
    UNIQUE (document_version_id, position)
) WITHOUT ROWID;

CREATE INDEX document_units_role_id ON document_units (role_id);

CREATE TRIGGER document_units_never_updated
BEFORE UPDATE ON document_units
BEGIN
    SELECT RAISE(ABORT, 'document_units rows are never changed');
END;

CREATE TRIGGER document_units_never_deleted
BEFORE DELETE ON document_units
BEGIN
    SELECT RAISE(ABORT, 'document_units rows are never deleted');
END;

CREATE TRIGGER document_units_never_replaced
BEFORE INSERT ON document_units
WHEN EXISTS (
    SELECT 1 FROM document_units
    WHERE id = NEW.id
        OR (document_version_id = NEW.document_version_id AND position = NEW.position)
)
BEGIN
    SELECT RAISE(ABORT, 'document_units rows are never replaced');
END;

-- The fact versions a unit cites. Indexed by fact for the "used in" links.
CREATE TABLE document_unit_facts (
    document_unit_id TEXT NOT NULL REFERENCES document_units (id),
    fact_id TEXT NOT NULL,
    fact_version_id TEXT NOT NULL,
    PRIMARY KEY (document_unit_id, fact_id),
    FOREIGN KEY (fact_id, fact_version_id) REFERENCES fact_versions (fact_id, id)
) WITHOUT ROWID;

CREATE INDEX document_unit_facts_fact_id ON document_unit_facts (fact_id);

-- Only a draft version gains units, citations and sent facts: an approved or sent version is
-- frozen. Exports are the exception (below).
CREATE TRIGGER document_units_only_in_drafts
BEFORE INSERT ON document_units
WHEN EXISTS (
    SELECT 1 FROM document_versions WHERE id = NEW.document_version_id AND status <> 'draft'
)
BEGIN
    SELECT RAISE(ABORT, 'only a draft version gains units');
END;

CREATE TRIGGER document_unit_facts_never_updated
BEFORE UPDATE ON document_unit_facts
BEGIN
    SELECT RAISE(ABORT, 'document_unit_facts rows are never changed');
END;

CREATE TRIGGER document_unit_facts_never_deleted
BEFORE DELETE ON document_unit_facts
BEGIN
    SELECT RAISE(ABORT, 'document_unit_facts rows are never deleted');
END;

CREATE TRIGGER document_unit_facts_never_replaced
BEFORE INSERT ON document_unit_facts
WHEN EXISTS (
    SELECT 1 FROM document_unit_facts
    WHERE document_unit_id = NEW.document_unit_id AND fact_id = NEW.fact_id
)
BEGIN
    SELECT RAISE(ABORT, 'document_unit_facts rows are never replaced');
END;

-- The input set of a generated version: the fact versions its request sent.
CREATE TABLE document_sent_facts (
    document_version_id TEXT NOT NULL REFERENCES document_versions (id),
    fact_id TEXT NOT NULL,
    fact_version_id TEXT NOT NULL,
    PRIMARY KEY (document_version_id, fact_id),
    FOREIGN KEY (fact_id, fact_version_id) REFERENCES fact_versions (fact_id, id)
) WITHOUT ROWID;

CREATE INDEX document_sent_facts_fact_id ON document_sent_facts (fact_id);

CREATE TRIGGER document_unit_facts_only_in_drafts
BEFORE INSERT ON document_unit_facts
WHEN EXISTS (
    SELECT 1 FROM document_units u JOIN document_versions v ON v.id = u.document_version_id
    WHERE u.id = NEW.document_unit_id AND v.status <> 'draft'
)
BEGIN
    SELECT RAISE(ABORT, 'only a draft version gains citations');
END;

CREATE TRIGGER document_sent_facts_never_updated
BEFORE UPDATE ON document_sent_facts
BEGIN
    SELECT RAISE(ABORT, 'document_sent_facts rows are never changed');
END;

CREATE TRIGGER document_sent_facts_never_deleted
BEFORE DELETE ON document_sent_facts
BEGIN
    SELECT RAISE(ABORT, 'document_sent_facts rows are never deleted');
END;

CREATE TRIGGER document_sent_facts_only_in_drafts
BEFORE INSERT ON document_sent_facts
WHEN EXISTS (
    SELECT 1 FROM document_versions WHERE id = NEW.document_version_id AND status <> 'draft'
)
BEGIN
    SELECT RAISE(ABORT, 'only a draft version gains sent facts');
END;

CREATE TRIGGER document_sent_facts_never_replaced
BEFORE INSERT ON document_sent_facts
WHEN EXISTS (
    SELECT 1 FROM document_sent_facts
    WHERE document_version_id = NEW.document_version_id AND fact_id = NEW.fact_id
)
BEGIN
    SELECT RAISE(ABORT, 'document_sent_facts rows are never replaced');
END;

-- A file rendered from a version. stored_name is code-built, unique (ignoring case, as file
-- systems may), and only letters, digits, dot, dash and underscore, never starting with a dot,
-- ending in a lowercase dot and the format; the file lives under documents/. The typeof and
-- instr checks matter: length() and GLOB stop at an embedded NUL, so without them
-- 'a.pdf' || char(0) || '../x' would pass.
CREATE TABLE document_exports (
    id TEXT PRIMARY KEY NOT NULL,
    document_version_id TEXT NOT NULL REFERENCES document_versions (id),
    format TEXT NOT NULL CHECK (format IN ('pdf', 'docx')),
    stored_name TEXT NOT NULL CHECK (
        typeof(stored_name) = 'text'
        AND instr(stored_name, char(0)) = 0
        AND length(stored_name) BETWEEN 1 AND 200
        AND substr(stored_name, 1, 1) <> '.'
        AND stored_name NOT GLOB '*[^A-Za-z0-9._-]*'
        AND substr(stored_name, -(length(format) + 1)) = '.' || format
        AND length(stored_name) > length(format) + 1
    ),
    sha256 TEXT NOT NULL CHECK (
        typeof(sha256) = 'text'
        AND instr(sha256, char(0)) = 0
        AND length(sha256) = 64
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    size_bytes INTEGER NOT NULL CHECK (typeof(size_bytes) = 'integer' AND size_bytes >= 0),
    pages INTEGER CHECK (pages IS NULL OR (typeof(pages) = 'integer' AND pages >= 1)),
    created_at TEXT NOT NULL,
    UNIQUE (stored_name COLLATE NOCASE)
) WITHOUT ROWID;

CREATE INDEX document_exports_document_version_id
ON document_exports (document_version_id, created_at);

CREATE TRIGGER document_exports_never_updated
BEFORE UPDATE ON document_exports
BEGIN
    SELECT RAISE(ABORT, 'document_exports rows are never changed');
END;

CREATE TRIGGER document_exports_never_deleted
BEFORE DELETE ON document_exports
BEGIN
    SELECT RAISE(ABORT, 'document_exports rows are never deleted');
END;

CREATE TRIGGER document_exports_never_replaced
BEFORE INSERT ON document_exports
WHEN EXISTS (
    SELECT 1 FROM document_exports
    WHERE id = NEW.id OR stored_name = NEW.stored_name COLLATE NOCASE
)
BEGIN
    SELECT RAISE(ABORT, 'document_exports rows are never replaced');
END;

-- What was sent when the application was submitted (M6.10): one row per application, written
-- once. Either version may be absent when that document had nothing to send; a version named
-- here belongs to this application and to a document of the matching kind.
CREATE TABLE application_submissions (
    application_id TEXT PRIMARY KEY NOT NULL REFERENCES applications (id),
    frozen_at TEXT NOT NULL,
    cv_version_id TEXT REFERENCES document_versions (id),
    letter_version_id TEXT REFERENCES document_versions (id),
    created_at TEXT NOT NULL
) WITHOUT ROWID;

CREATE TRIGGER application_submissions_never_updated
BEFORE UPDATE ON application_submissions
BEGIN
    SELECT RAISE(ABORT, 'application_submissions rows are never changed');
END;

CREATE TRIGGER application_submissions_never_deleted
BEFORE DELETE ON application_submissions
BEGIN
    SELECT RAISE(ABORT, 'application_submissions rows are never deleted');
END;

CREATE TRIGGER application_submissions_never_replaced
BEFORE INSERT ON application_submissions
WHEN EXISTS (SELECT 1 FROM application_submissions WHERE application_id = NEW.application_id)
BEGIN
    SELECT RAISE(ABORT, 'application_submissions rows are never replaced');
END;

CREATE TRIGGER application_submissions_versions_match
BEFORE INSERT ON application_submissions
WHEN (
    NEW.cv_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM document_versions v JOIN documents d ON d.id = v.document_id
        WHERE v.id = NEW.cv_version_id AND d.application_id = NEW.application_id
            AND d.kind = 'cv'
    )
) OR (
    NEW.letter_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM document_versions v JOIN documents d ON d.id = v.document_id
        WHERE v.id = NEW.letter_version_id AND d.application_id = NEW.application_id
            AND d.kind = 'cover_letter'
    )
)
BEGIN
    SELECT RAISE(ABORT, 'a submission names only versions of its own documents');
END;

-- An application is untouched, and Undo may remove it, only while it has none of the rows above
-- either. Same message as in 0005.
DROP TRIGGER applications_delete_only_untouched;
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
    OR EXISTS (SELECT 1 FROM workspaces WHERE application_id = OLD.id)
    OR EXISTS (SELECT 1 FROM gap_checks WHERE application_id = OLD.id)
    OR EXISTS (SELECT 1 FROM angle_proposals WHERE application_id = OLD.id)
    OR EXISTS (SELECT 1 FROM documents WHERE application_id = OLD.id)
    OR EXISTS (SELECT 1 FROM application_submissions WHERE application_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'this application already has changes');
END;
