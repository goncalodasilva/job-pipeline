-- M8.2 part B (ADR-124 item 5, as amended): the fact versions SENT for a narrative version,
-- the parity with document_sent_facts. A line of a card is re-checked against the facts its
-- version held plus the facts the person saved from documents. 0008 is not edited.

CREATE TABLE prep_version_facts (
    version_id TEXT NOT NULL REFERENCES prep_document_versions(id),
    fact_id TEXT NOT NULL,
    fact_version_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1),
    PRIMARY KEY(version_id,fact_id),
    FOREIGN KEY(fact_id,fact_version_id) REFERENCES fact_versions(fact_id,id)
) WITHOUT ROWID;

CREATE INDEX prep_version_facts_fact ON prep_version_facts(fact_id);

CREATE TRIGGER prep_version_facts_never_replaced BEFORE INSERT ON prep_version_facts
WHEN EXISTS(SELECT 1 FROM prep_version_facts WHERE version_id=NEW.version_id AND fact_id=NEW.fact_id)
BEGIN SELECT RAISE(ABORT,'prep rows are never replaced'); END;

CREATE TRIGGER prep_version_facts_draft_only BEFORE INSERT ON prep_version_facts
WHEN NOT EXISTS(SELECT 1 FROM prep_document_versions WHERE id=NEW.version_id AND status='draft')
BEGIN SELECT RAISE(ABORT,'only a draft prep version gains sent facts'); END;

CREATE TRIGGER prep_version_facts_no_update BEFORE UPDATE ON prep_version_facts
BEGIN SELECT RAISE(ABORT,'prep history is immutable'); END;

CREATE TRIGGER prep_version_facts_no_delete BEFORE DELETE ON prep_version_facts
BEGIN SELECT RAISE(ABORT,'prep history is immutable'); END;
