"""0008 bounds the free-text columns that hold copies of CV text and model metadata."""

import sqlite3

import pytest

from tests.integration.test_schema_0004 import insert
from tests.integration.test_schema_0008 import db, rows, seed_prep  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("table", "field", "value"),
    [
        pytest.param("practice_attempts", "cv_snapshot", "x" * 100_001, id="cv_snapshot_cap"),
        pytest.param("practice_attempts", "cv_snapshot", "a\x00b", id="cv_snapshot_nul"),
        pytest.param("practice_attempts", "fact_refs", "[" + "1," * 10_000 + "1]", id="refs_cap"),
        pytest.param("practice_attempts", "provider", "p" * 101, id="attempt_provider"),
        pytest.param("practice_attempts", "model", "m" * 201, id="attempt_model"),
        pytest.param("practice_attempts", "prompt_id", "a\x00b", id="attempt_prompt_nul"),
        pytest.param("practice_attempts", "prompt_version", "9" * 51, id="attempt_version"),
        pytest.param("bank_questions", "provider", "p" * 101, id="bank_provider"),
        pytest.param("bank_question_sightings", "model", "m" * 201, id="sighting_model"),
        pytest.param("prep_document_versions", "prompt_id", "i" * 101, id="document_prompt"),
        pytest.param("prep_units", "added_details", "[" + '"a",' * 6_000 + '"a"]', id="details"),
        pytest.param("prep_units", "flags", '["a\x00b"]', id="flags_nul"),
        pytest.param("company_briefs", "prompt_version", "v" * 51, id="brief_version"),
    ],
)
def test_0008_free_text_columns_are_bounded(db, table, field, value):  # noqa: F811
    seed_prep(db)
    base = rows()
    if table not in base:
        pytest.skip("seed row not defined for this table")
    row = dict(base[table]) | {field: value}
    if table in {"bank_questions", "bank_question_sightings"}:
        row["id"] = "q-new" if table == "bank_questions" else "s-new"
        if table == "bank_question_sightings":
            row["question_id"] = "q1"
    elif table in {"practice_attempts", "prep_units", "prep_document_versions", "company_briefs"}:
        row["id"] = "new-" + table
        if table == "prep_units":
            row["position"] = 2
        if table == "prep_document_versions":
            row["number"] = 2
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        insert(conn, table, row)


def test_0008_ordinary_values_still_fit(db):  # noqa: F811
    seed_prep(db)
    row = dict(rows()["practice_attempts"]) | {
        "id": "a2",
        "cv_snapshot": "Fictional CV wording.",
        "provider": "claude",
        "model": "fictional-model",
        "prompt_id": "answer_feedback",
        "prompt_version": "1",
    }
    with db.transaction() as conn:
        insert(conn, "practice_attempts", row)
