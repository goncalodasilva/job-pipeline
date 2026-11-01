-- M8 interview preparation (ADR-124, ADR-128). All wording is untrusted local data.
-- Explicit keys, WITHOUT ROWID, bounded text, version counters and replacement guards.


CREATE UNIQUE INDEX events_application_identity ON events(application_id,id);

CREATE TABLE bank_questions (
    id TEXT PRIMARY KEY NOT NULL,
    text TEXT NOT NULL CHECK(typeof(text)='text' AND length(text)<=1000 AND instr(text,char(0))=0 AND trim(text,char(32,9,10,13))<>''),
    type TEXT NOT NULL CHECK(type IN ('behavioral','technical','cv','ask_them')),
    origin TEXT NOT NULL CHECK(origin IN ('generated','asked','reported_online','user')),
    company_id TEXT REFERENCES companies(id),
    role_family TEXT NOT NULL DEFAULT '' CHECK(typeof(role_family)='text' AND length(role_family)<=200 AND instr(role_family,char(0))=0),
    seniority TEXT NOT NULL DEFAULT '' CHECK(typeof(seniority)='text' AND length(seniority)<=100 AND instr(seniority,char(0))=0),
    asked_count INTEGER NOT NULL DEFAULT 0 CHECK(typeof(asked_count)='integer' AND asked_count>=0),
    source_url TEXT CHECK(source_url IS NULL OR (typeof(source_url)='text' AND length(source_url)<=2000 AND instr(source_url,char(0))=0)),
    found_at TEXT,
    source_date TEXT CHECK(source_date IS NULL OR (typeof(source_date)='text' AND length(source_date)<=100 AND instr(source_date,char(0))=0)),
    provider TEXT CHECK(provider IS NULL OR (typeof(provider)='text' AND length(provider)<=100 AND instr(provider,char(0))=0)), model TEXT CHECK(model IS NULL OR (typeof(model)='text' AND length(model)<=200 AND instr(model,char(0))=0)), prompt_id TEXT CHECK(prompt_id IS NULL OR (typeof(prompt_id)='text' AND length(prompt_id)<=100 AND instr(prompt_id,char(0))=0)), prompt_version TEXT CHECK(prompt_version IS NULL OR (typeof(prompt_version)='text' AND length(prompt_version)<=50 AND instr(prompt_version,char(0))=0)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1),
    CHECK(origin<>'reported_online' OR (source_url IS NOT NULL AND found_at IS NOT NULL)),
    CHECK(source_url IS NULL OR source_url GLOB 'https://?*' OR source_url GLOB 'http://?*')
) WITHOUT ROWID;

CREATE TRIGGER bank_questions_never_replaced BEFORE INSERT ON bank_questions
WHEN EXISTS(SELECT 1 FROM bank_questions WHERE id=NEW.id)
BEGIN SELECT RAISE(ABORT,'prep rows are never replaced'); END;

CREATE TRIGGER bank_questions_identity_kept BEFORE UPDATE ON bank_questions
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'prep identity is stable'); END;

CREATE TABLE bank_question_requirements (
    question_id TEXT NOT NULL REFERENCES bank_questions(id),
    text TEXT NOT NULL CHECK(typeof(text)='text' AND length(text)<=1000 AND instr(text,char(0))=0 AND trim(text,char(32,9,10,13))<>''),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1), PRIMARY KEY(question_id,text)
) WITHOUT ROWID;

CREATE TRIGGER bank_question_requirements_never_replaced BEFORE INSERT ON bank_question_requirements
WHEN EXISTS(SELECT 1 FROM bank_question_requirements WHERE question_id=NEW.question_id AND text=NEW.text)
BEGIN SELECT RAISE(ABORT,'prep rows are never replaced'); END;

CREATE TRIGGER bank_question_requirements_identity_kept BEFORE UPDATE ON bank_question_requirements
WHEN NEW.question_id IS NOT OLD.question_id OR NEW.text IS NOT OLD.text OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'prep identity is stable'); END;

CREATE TABLE bank_question_sightings (
    id TEXT PRIMARY KEY NOT NULL,
    question_id TEXT NOT NULL REFERENCES bank_questions(id),
    application_id TEXT REFERENCES applications(id),
    event_id TEXT REFERENCES events(id),
    origin TEXT NOT NULL CHECK(origin IN ('generated','asked','reported_online','user')),
    source_url TEXT CHECK(source_url IS NULL OR (typeof(source_url)='text' AND length(source_url)<=2000 AND instr(source_url,char(0))=0)),
    source_date TEXT CHECK(source_date IS NULL OR (typeof(source_date)='text' AND length(source_date)<=100 AND instr(source_date,char(0))=0)),
    provider TEXT CHECK(provider IS NULL OR (typeof(provider)='text' AND length(provider)<=100 AND instr(provider,char(0))=0)), model TEXT CHECK(model IS NULL OR (typeof(model)='text' AND length(model)<=200 AND instr(model,char(0))=0)), prompt_id TEXT CHECK(prompt_id IS NULL OR (typeof(prompt_id)='text' AND length(prompt_id)<=100 AND instr(prompt_id,char(0))=0)), prompt_version TEXT CHECK(prompt_version IS NULL OR (typeof(prompt_version)='text' AND length(prompt_version)<=50 AND instr(prompt_version,char(0))=0)),
    task_id TEXT REFERENCES tasks(id),
    found_at TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1)
) WITHOUT ROWID;

CREATE TRIGGER bank_question_sightings_never_replaced BEFORE INSERT ON bank_question_sightings
WHEN EXISTS(SELECT 1 FROM bank_question_sightings WHERE id=NEW.id)
BEGIN SELECT RAISE(ABORT,'prep rows are never replaced'); END;

CREATE TRIGGER bank_question_sightings_identity_kept BEFORE UPDATE ON bank_question_sightings
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'prep identity is stable'); END;

CREATE TABLE prep_items (
    id TEXT PRIMARY KEY NOT NULL,
    application_id TEXT NOT NULL REFERENCES applications(id),
    question_id TEXT NOT NULL REFERENCES bank_questions(id),
    ready INTEGER NOT NULL DEFAULT 0 CHECK(typeof(ready)='integer' AND ready IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1), UNIQUE(application_id,question_id)
) WITHOUT ROWID;

CREATE TRIGGER prep_items_never_replaced BEFORE INSERT ON prep_items
WHEN EXISTS(SELECT 1 FROM prep_items WHERE id=NEW.id OR (application_id=NEW.application_id AND question_id=NEW.question_id))
BEGIN SELECT RAISE(ABORT,'prep rows are never replaced'); END;

CREATE TRIGGER prep_items_identity_kept BEFORE UPDATE ON prep_items
WHEN NEW.id IS NOT OLD.id OR NEW.application_id IS NOT OLD.application_id OR NEW.question_id IS NOT OLD.question_id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'prep identity is stable'); END;

CREATE TABLE practice_attempts (
    id TEXT PRIMARY KEY NOT NULL,
    prep_item_id TEXT NOT NULL REFERENCES prep_items(id),
    answer_text TEXT NOT NULL CHECK(typeof(answer_text)='text' AND length(answer_text)<=4000 AND instr(answer_text,char(0))=0 AND trim(answer_text,char(32,9,10,13))<>''),
    feedback TEXT CHECK(feedback IS NULL OR (json_valid(feedback) AND length(feedback)<=50000 AND instr(feedback,char(0))=0)),
    fact_refs TEXT NOT NULL DEFAULT '[]' CHECK(CASE WHEN json_valid(fact_refs) THEN json_type(fact_refs)='array' ELSE 0 END AND length(fact_refs)<=20000 AND instr(fact_refs,char(0))=0),
    cv_snapshot TEXT NOT NULL DEFAULT '' CHECK(typeof(cv_snapshot)='text' AND length(cv_snapshot)<=100000 AND instr(cv_snapshot,char(0))=0),
    provider TEXT CHECK(provider IS NULL OR (typeof(provider)='text' AND length(provider)<=100 AND instr(provider,char(0))=0)), model TEXT CHECK(model IS NULL OR (typeof(model)='text' AND length(model)<=200 AND instr(model,char(0))=0)), prompt_id TEXT CHECK(prompt_id IS NULL OR (typeof(prompt_id)='text' AND length(prompt_id)<=100 AND instr(prompt_id,char(0))=0)), prompt_version TEXT CHECK(prompt_version IS NULL OR (typeof(prompt_version)='text' AND length(prompt_version)<=50 AND instr(prompt_version,char(0))=0)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1)
) WITHOUT ROWID;

CREATE TRIGGER practice_attempts_never_replaced BEFORE INSERT ON practice_attempts
WHEN EXISTS(SELECT 1 FROM practice_attempts WHERE id=NEW.id)
BEGIN SELECT RAISE(ABORT,'prep rows are never replaced'); END;

CREATE TRIGGER practice_attempts_identity_kept BEFORE UPDATE ON practice_attempts
WHEN NEW.id IS NOT OLD.id OR NEW.prep_item_id IS NOT OLD.prep_item_id OR NEW.answer_text IS NOT OLD.answer_text OR NEW.cv_snapshot IS NOT OLD.cv_snapshot OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'prep identity is stable'); END;

CREATE TABLE prep_documents (
    id TEXT PRIMARY KEY NOT NULL,
    application_id TEXT NOT NULL REFERENCES applications(id),
    kind TEXT NOT NULL CHECK(kind IN ('tell_me_about_yourself','why_this_role','why_this_company','why_leaving','strengths','weaknesses','salary','adapted_answer')),
    question_id TEXT REFERENCES bank_questions(id),
    current_version_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1),
    CHECK((kind='adapted_answer')=(question_id IS NOT NULL)),
    FOREIGN KEY(id,current_version_id) REFERENCES prep_document_versions(document_id,id) DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;

CREATE TRIGGER prep_documents_never_replaced BEFORE INSERT ON prep_documents
WHEN EXISTS(SELECT 1 FROM prep_documents WHERE id=NEW.id OR (application_id=NEW.application_id AND kind=NEW.kind AND (kind<>'adapted_answer' OR question_id=NEW.question_id)))
BEGIN SELECT RAISE(ABORT,'prep rows are never replaced'); END;

CREATE TRIGGER prep_documents_identity_kept BEFORE UPDATE ON prep_documents
WHEN NEW.id IS NOT OLD.id OR NEW.application_id IS NOT OLD.application_id OR NEW.kind IS NOT OLD.kind OR NEW.question_id IS NOT OLD.question_id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'prep identity is stable'); END;

CREATE TABLE prep_document_versions (
    id TEXT PRIMARY KEY NOT NULL,
    document_id TEXT NOT NULL REFERENCES prep_documents(id),
    number INTEGER NOT NULL CHECK(typeof(number)='integer' AND number>=1),
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved')),
    origin TEXT NOT NULL CHECK(origin IN ('generated','user','reused','code')),
    angle_id TEXT REFERENCES angles(id),
    angle_text TEXT NOT NULL DEFAULT '' CHECK(typeof(angle_text)='text' AND length(angle_text)<=4000 AND instr(angle_text,char(0))=0),
    parent_version_id TEXT,
    reused_version_id TEXT REFERENCES prep_document_versions(id),
    provider TEXT CHECK(provider IS NULL OR (typeof(provider)='text' AND length(provider)<=100 AND instr(provider,char(0))=0)), model TEXT CHECK(model IS NULL OR (typeof(model)='text' AND length(model)<=200 AND instr(model,char(0))=0)), prompt_id TEXT CHECK(prompt_id IS NULL OR (typeof(prompt_id)='text' AND length(prompt_id)<=100 AND instr(prompt_id,char(0))=0)), prompt_version TEXT CHECK(prompt_version IS NULL OR (typeof(prompt_version)='text' AND length(prompt_version)<=50 AND instr(prompt_version,char(0))=0)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1),
    UNIQUE(document_id,number), UNIQUE(document_id,id),
    FOREIGN KEY(document_id,parent_version_id) REFERENCES prep_document_versions(document_id,id)
) WITHOUT ROWID;

CREATE TRIGGER prep_document_versions_never_replaced BEFORE INSERT ON prep_document_versions
WHEN EXISTS(SELECT 1 FROM prep_document_versions WHERE id=NEW.id OR (document_id=NEW.document_id AND number=NEW.number))
BEGIN SELECT RAISE(ABORT,'prep rows are never replaced'); END;

CREATE TRIGGER prep_document_versions_identity_kept BEFORE UPDATE ON prep_document_versions
WHEN NEW.id IS NOT OLD.id OR NEW.document_id IS NOT OLD.document_id OR NEW.number IS NOT OLD.number OR NEW.origin IS NOT OLD.origin OR NEW.angle_id IS NOT OLD.angle_id OR NEW.angle_text IS NOT OLD.angle_text OR NEW.parent_version_id IS NOT OLD.parent_version_id OR NEW.reused_version_id IS NOT OLD.reused_version_id OR NEW.provider IS NOT OLD.provider OR NEW.model IS NOT OLD.model OR NEW.prompt_id IS NOT OLD.prompt_id OR NEW.prompt_version IS NOT OLD.prompt_version OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'prep identity is stable'); END;

CREATE TABLE prep_units (
    id TEXT PRIMARY KEY NOT NULL,
    document_version_id TEXT NOT NULL REFERENCES prep_document_versions(id),
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=1),
    kind TEXT NOT NULL DEFAULT 'content' CHECK(kind IN ('content','structure')),
    section TEXT NOT NULL DEFAULT 'answer' CHECK(typeof(section)='text' AND length(section)<=200 AND instr(section,char(0))=0),
    role_id TEXT REFERENCES roles(id),
    text TEXT NOT NULL CHECK(typeof(text)='text' AND length(text)<=4000 AND instr(text,char(0))=0 AND trim(text,char(32,9,10,13))<>''),
    origin TEXT NOT NULL DEFAULT 'ai' CHECK(origin IN ('ai','user','code')),
    added_details TEXT NOT NULL DEFAULT '[]' CHECK(CASE WHEN json_valid(added_details) THEN json_type(added_details)='array' ELSE 0 END AND length(added_details)<=20000 AND instr(added_details,char(0))=0),
    flags TEXT NOT NULL DEFAULT '[]' CHECK(CASE WHEN json_valid(flags) THEN json_type(flags)='array' ELSE 0 END AND length(flags)<=20000 AND instr(flags,char(0))=0),
    removed INTEGER NOT NULL DEFAULT 0 CHECK(typeof(removed)='integer' AND removed IN(0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1), UNIQUE(document_version_id,position)
) WITHOUT ROWID;

CREATE TRIGGER prep_units_never_replaced BEFORE INSERT ON prep_units
WHEN EXISTS(SELECT 1 FROM prep_units WHERE id=NEW.id OR (document_version_id=NEW.document_version_id AND position=NEW.position))
BEGIN SELECT RAISE(ABORT,'prep rows are never replaced'); END;

CREATE TRIGGER prep_units_identity_kept BEFORE UPDATE ON prep_units
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'prep identity is stable'); END;

CREATE TABLE prep_unit_facts (
    unit_id TEXT NOT NULL REFERENCES prep_units(id),
    fact_id TEXT NOT NULL, fact_version_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1), PRIMARY KEY(unit_id,fact_id),
    FOREIGN KEY(fact_id,fact_version_id) REFERENCES fact_versions(fact_id,id)
) WITHOUT ROWID;

CREATE TRIGGER prep_unit_facts_never_replaced BEFORE INSERT ON prep_unit_facts
WHEN EXISTS(SELECT 1 FROM prep_unit_facts WHERE unit_id=NEW.unit_id AND fact_id=NEW.fact_id)
BEGIN SELECT RAISE(ABORT,'prep rows are never replaced'); END;

CREATE TRIGGER prep_unit_facts_identity_kept BEFORE UPDATE ON prep_unit_facts
WHEN NEW.unit_id IS NOT OLD.unit_id OR NEW.fact_id IS NOT OLD.fact_id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'prep identity is stable'); END;

CREATE TABLE debriefs (
    id TEXT PRIMARY KEY NOT NULL,
    application_id TEXT NOT NULL REFERENCES applications(id),
    event_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','done','dismissed')),
    questions_text TEXT NOT NULL DEFAULT '' CHECK(typeof(questions_text)='text' AND length(questions_text)<=4000 AND instr(questions_text,char(0))=0),
    how_it_went TEXT NOT NULL DEFAULT '' CHECK(typeof(how_it_went)='text' AND length(how_it_went)<=4000 AND instr(how_it_went,char(0))=0),
    experiences_text TEXT NOT NULL DEFAULT '' CHECK(typeof(experiences_text)='text' AND length(experiences_text)<=4000 AND instr(experiences_text,char(0))=0),
    company_only INTEGER NOT NULL DEFAULT 0 CHECK(typeof(company_only)='integer' AND company_only IN (0,1)),
    next_step TEXT CHECK(next_step IN ('next_round','offer','rejected','other')),
    next_event_id TEXT REFERENCES events(id),
    status_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(typeof(status_confirmed)='integer' AND status_confirmed IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1),
    FOREIGN KEY(application_id,event_id) REFERENCES events(application_id,id)
) WITHOUT ROWID;

CREATE TRIGGER debriefs_never_replaced BEFORE INSERT ON debriefs
WHEN EXISTS(SELECT 1 FROM debriefs WHERE id=NEW.id OR event_id=NEW.event_id)
BEGIN SELECT RAISE(ABORT,'prep rows are never replaced'); END;

CREATE TRIGGER debriefs_identity_kept BEFORE UPDATE ON debriefs
WHEN NEW.id IS NOT OLD.id OR NEW.application_id IS NOT OLD.application_id OR NEW.event_id IS NOT OLD.event_id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'prep identity is stable'); END;

CREATE TABLE debrief_drafts (
    id TEXT PRIMARY KEY NOT NULL,
    debrief_id TEXT NOT NULL REFERENCES debriefs(id),
    fact_id TEXT NOT NULL REFERENCES facts(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1), UNIQUE(debrief_id,fact_id)
) WITHOUT ROWID;

CREATE TRIGGER debrief_drafts_never_replaced BEFORE INSERT ON debrief_drafts
WHEN EXISTS(SELECT 1 FROM debrief_drafts WHERE id=NEW.id OR (debrief_id=NEW.debrief_id AND fact_id=NEW.fact_id))
BEGIN SELECT RAISE(ABORT,'prep rows are never replaced'); END;

CREATE TRIGGER debrief_drafts_identity_kept BEFORE UPDATE ON debrief_drafts
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'prep identity is stable'); END;

CREATE TABLE company_briefs (
    id TEXT PRIMARY KEY NOT NULL,
    company_id TEXT NOT NULL REFERENCES companies(id),
    content TEXT NOT NULL CHECK(CASE WHEN json_valid(content) THEN json_type(content)='object' AND length(content)<=50000 AND instr(content,char(0))=0 ELSE 0 END),
    sources TEXT NOT NULL CHECK(CASE WHEN json_valid(sources) THEN json_type(sources)='array' AND json_array_length(sources)<=50 ELSE 0 END),
    found_at TEXT NOT NULL,
    provider TEXT CHECK(provider IS NULL OR (typeof(provider)='text' AND length(provider)<=100 AND instr(provider,char(0))=0)), model TEXT CHECK(model IS NULL OR (typeof(model)='text' AND length(model)<=200 AND instr(model,char(0))=0)), prompt_id TEXT CHECK(prompt_id IS NULL OR (typeof(prompt_id)='text' AND length(prompt_id)<=100 AND instr(prompt_id,char(0))=0)), prompt_version TEXT CHECK(prompt_version IS NULL OR (typeof(prompt_version)='text' AND length(prompt_version)<=50 AND instr(prompt_version,char(0))=0)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1)
) WITHOUT ROWID;

CREATE TRIGGER company_briefs_never_replaced BEFORE INSERT ON company_briefs
WHEN EXISTS(SELECT 1 FROM company_briefs WHERE id=NEW.id)
BEGIN SELECT RAISE(ABORT,'prep rows are never replaced'); END;

CREATE TRIGGER company_briefs_identity_kept BEFORE UPDATE ON company_briefs
WHEN NEW.id IS NOT OLD.id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'prep identity is stable'); END;

CREATE UNIQUE INDEX prep_documents_narrative ON prep_documents(application_id,kind) WHERE kind<>'adapted_answer';
CREATE UNIQUE INDEX prep_documents_adapted ON prep_documents(application_id,question_id) WHERE kind='adapted_answer';
CREATE INDEX bank_questions_company ON bank_questions(company_id);
CREATE INDEX bank_sightings_question ON bank_question_sightings(question_id,found_at);
CREATE INDEX prep_items_question ON prep_items(question_id);
CREATE INDEX practice_attempts_item ON practice_attempts(prep_item_id,created_at);
CREATE INDEX prep_units_version ON prep_units(document_version_id,position);
CREATE INDEX prep_unit_facts_fact ON prep_unit_facts(fact_id);
CREATE INDEX debriefs_application ON debriefs(application_id);
CREATE INDEX company_briefs_company ON company_briefs(company_id,found_at);
CREATE VIRTUAL TABLE bank_questions_fts USING fts5(id UNINDEXED,text);
CREATE TRIGGER bank_questions_fts_insert AFTER INSERT ON bank_questions
BEGIN INSERT INTO bank_questions_fts(id,text) VALUES(NEW.id,NEW.text); END;
CREATE TRIGGER bank_questions_fts_update AFTER UPDATE OF text ON bank_questions
BEGIN DELETE FROM bank_questions_fts WHERE id=OLD.id;
INSERT INTO bank_questions_fts(id,text) VALUES(NEW.id,NEW.text); END;
CREATE TRIGGER bank_questions_fts_delete AFTER DELETE ON bank_questions
BEGIN DELETE FROM bank_questions_fts WHERE id=OLD.id; END;
CREATE TRIGGER prep_versions_start_draft BEFORE INSERT ON prep_document_versions WHEN NEW.status<>'draft'
BEGIN SELECT RAISE(ABORT,'prep versions start draft'); END;
CREATE TRIGGER prep_versions_forward BEFORE UPDATE OF status ON prep_document_versions
WHEN NEW.status IS NOT OLD.status AND NOT(OLD.status='draft' AND NEW.status='approved')
BEGIN SELECT RAISE(ABORT,'prep approval is forward only'); END;
CREATE TRIGGER prep_units_draft_only BEFORE INSERT ON prep_units
WHEN NOT EXISTS(SELECT 1 FROM prep_document_versions WHERE id=NEW.document_version_id AND status='draft')
BEGIN SELECT RAISE(ABORT,'approved prep content is immutable'); END;
CREATE TRIGGER prep_unit_facts_draft_only BEFORE INSERT ON prep_unit_facts
WHEN NOT EXISTS(SELECT 1 FROM prep_units u JOIN prep_document_versions v ON v.id=u.document_version_id WHERE u.id=NEW.unit_id AND v.status='draft')
BEGIN SELECT RAISE(ABORT,'approved prep citations are immutable'); END;

CREATE TRIGGER bank_question_requirements_no_update BEFORE UPDATE ON bank_question_requirements
BEGIN SELECT RAISE(ABORT,'prep history is immutable'); END;

CREATE TRIGGER bank_question_requirements_no_delete BEFORE DELETE ON bank_question_requirements
BEGIN SELECT RAISE(ABORT,'prep history is immutable'); END;

CREATE TRIGGER bank_question_sightings_no_update BEFORE UPDATE ON bank_question_sightings
BEGIN SELECT RAISE(ABORT,'prep history is immutable'); END;

CREATE TRIGGER bank_question_sightings_no_delete BEFORE DELETE ON bank_question_sightings
BEGIN SELECT RAISE(ABORT,'prep history is immutable'); END;

CREATE TRIGGER prep_units_no_update BEFORE UPDATE ON prep_units
BEGIN SELECT RAISE(ABORT,'prep history is immutable'); END;

CREATE TRIGGER prep_units_no_delete BEFORE DELETE ON prep_units
BEGIN SELECT RAISE(ABORT,'prep history is immutable'); END;

CREATE TRIGGER prep_unit_facts_no_update BEFORE UPDATE ON prep_unit_facts
BEGIN SELECT RAISE(ABORT,'prep history is immutable'); END;

CREATE TRIGGER prep_unit_facts_no_delete BEFORE DELETE ON prep_unit_facts
BEGIN SELECT RAISE(ABORT,'prep history is immutable'); END;

CREATE TRIGGER debrief_drafts_no_update BEFORE UPDATE ON debrief_drafts
BEGIN SELECT RAISE(ABORT,'prep history is immutable'); END;

CREATE TRIGGER debrief_drafts_no_delete BEFORE DELETE ON debrief_drafts
BEGIN SELECT RAISE(ABORT,'prep history is immutable'); END;

CREATE TRIGGER prep_versions_no_delete BEFORE DELETE ON prep_document_versions BEGIN SELECT RAISE(ABORT,'prep history is immutable'); END;

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
    OR EXISTS(SELECT 1 FROM prep_items WHERE application_id=OLD.id)
    OR EXISTS(SELECT 1 FROM prep_documents WHERE application_id=OLD.id)
    OR EXISTS(SELECT 1 FROM debriefs WHERE application_id=OLD.id)
    OR EXISTS(SELECT 1 FROM bank_question_sightings WHERE application_id=OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'this application already has changes');
END;

