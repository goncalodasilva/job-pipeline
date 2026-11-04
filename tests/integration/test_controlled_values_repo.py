"""M9.2 part D, storage level: the controlled-value editor's SQL (ADR-131 items 6-7; ADR-125
item 5; database rules: updates check ``version``).

``app.modules.storage.controlled_values_repo`` (new; callers own the transaction) works on the
existing ``controlled_values`` table (migration 0004: ``field``, ``value``, ``aliases`` JSON,
``version``, UNIQUE(field, value)) and reads ``jobs``:

- ``add_value(conn, *, value_id, field, value, now) -> None`` inserts a row (aliases ``[]``,
  version 1). The text is stored as given (the service applies the key rule). A second row with
  the same field and value is a ``sqlite3.IntegrityError``.
- ``set_aliases(conn, value_id, aliases, *, expected_version, now) -> bool`` replaces the alias
  list and bumps ``version`` and ``updated_at``; False (and nothing changes) when the row is
  missing or the version is stale.
- ``jobs_holding(conn, field, value) -> int`` counts the ``jobs`` rows whose column ``field`` is
  ``value``, whatever their triage state and whether or not the field is user-corrected.
- ``delete_unused(conn, value_id, *, expected_version) -> int`` deletes the row only when no job
  holds its value for its field, in one statement with the version check, and returns the number
  of jobs that hold it (0 means it was deleted). Unknown id: ``LookupError``; stale version:
  ``VersionConflict`` (checked before the use count).
- ``values_with_use(conn, field) -> tuple[dict, ...]`` lists the rows of a field by value:
  ``id``, ``field``, ``value``, ``aliases`` (a list), ``version`` and ``jobs`` (the use count).
- The editable fields are ``industry``, ``seniority`` and ``employment_type``: ``add_value``,
  ``set_aliases`` and ``delete_unused`` raise ``ValueError`` for any other field or for a row of
  another field (``work_mode`` is tied to ``WorkMode``) and change nothing; the field is never
  put into SQL unchecked.

Real SQLite in a temporary folder, the frozen clock, no network. Every value is fictional.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import VersionConflict
from app.modules.storage import Database
from tests.integration.test_portability import fresh
from tests.job_fixtures import generate_jobs, seed_jobs

pytestmark = pytest.mark.integration

NOW = "2026-11-04T09:00:00+00:00"
LATER = "2026-11-04T10:00:00+00:00"


def repo() -> Any:
    from app.modules.storage import controlled_values_repo

    return controlled_values_repo


def row(db: Database, value_id: str) -> dict[str, Any] | None:
    with db.read() as conn:
        found = conn.execute(
            "SELECT id, field, value, aliases, version, updated_at FROM controlled_values "
            "WHERE id=?",
            (value_id,),
        ).fetchone()
    names = ("id", "field", "value", "aliases", "version", "updated_at")
    return None if found is None else dict(zip(names, tuple(found), strict=True))


def table(db: Database) -> list[tuple[Any, ...]]:
    with db.read() as conn:
        return [tuple(r) for r in conn.execute("SELECT * FROM controlled_values ORDER BY id")]


def hold(db: Database, field: str, value: str, count: int, *, corrected: bool = False) -> None:
    """Make ``count`` fictional jobs hold ``value`` in ``field``."""
    seed_jobs(db, generate_jobs(count))
    with db.transaction() as conn:
        conn.execute(f"UPDATE jobs SET {field}=?", (value,))  # noqa: S608 - fixed test fields
        if corrected:
            conn.execute("UPDATE jobs SET user_corrected=?", (json.dumps([field]),))


# --- add -------------------------------------------------------------------------------------


def test_add_value_inserts_an_empty_alias_row_at_version_one(tmp_path: Path) -> None:
    db = fresh(tmp_path)
    try:
        with db.transaction() as conn:
            repo().add_value(
                conn, value_id="cv-new-1", field="industry", value="aerospace", now=NOW
            )
        assert row(db, "cv-new-1") == {
            "id": "cv-new-1",
            "field": "industry",
            "value": "aerospace",
            "aliases": "[]",
            "version": 1,
            "updated_at": NOW,
        }
    finally:
        db.close()


def test_add_value_refuses_an_exact_duplicate_in_the_same_field_only(tmp_path: Path) -> None:
    db = fresh(tmp_path)
    try:
        with db.transaction() as conn:
            repo().add_value(
                conn, value_id="cv-new-1", field="industry", value="aerospace", now=NOW
            )
            repo().add_value(
                conn, value_id="cv-new-2", field="seniority", value="aerospace", now=NOW
            )
        before = table(db)
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            repo().add_value(
                conn, value_id="cv-new-3", field="industry", value="aerospace", now=NOW
            )
        assert table(db) == before, "the failed add left nothing behind"
    finally:
        db.close()


@pytest.mark.parametrize("field", ["work_mode", "department", "", "industry; DROP TABLE jobs"])
def test_add_value_refuses_a_field_that_is_not_editable(tmp_path: Path, field: str) -> None:
    db = fresh(tmp_path)
    try:
        before = table(db)
        with pytest.raises(ValueError), db.transaction() as conn:
            repo().add_value(conn, value_id="cv-new-1", field=field, value="x", now=NOW)
        assert table(db) == before
    finally:
        db.close()


# --- replace aliases, with the version -------------------------------------------------------


def test_set_aliases_replaces_the_list_and_bumps_version_and_time(tmp_path: Path) -> None:
    db = fresh(tmp_path)
    try:
        value_id = "cv-industry-agriculture"
        before = row(db, value_id)
        assert before is not None and json.loads(before["aliases"]) == ["farming", "horticulture"]
        with db.transaction() as conn:
            assert repo().set_aliases(
                conn, value_id, ["agri", "growing"], expected_version=1, now=LATER
            )
        after = row(db, value_id)
        assert after is not None
        assert json.loads(after["aliases"]) == ["agri", "growing"], "replaced, not appended"
        assert (after["version"], after["updated_at"]) == (2, LATER)
        with db.transaction() as conn:
            assert repo().set_aliases(conn, value_id, [], expected_version=2, now=LATER)
        emptied = row(db, value_id)
        assert emptied is not None and emptied["aliases"] == "[]" and emptied["version"] == 3
    finally:
        db.close()


def test_set_aliases_with_a_stale_or_unknown_version_changes_nothing(tmp_path: Path) -> None:
    db = fresh(tmp_path)
    try:
        before = table(db)
        for version in (0, 2, 99):
            with db.transaction() as conn:
                assert not repo().set_aliases(
                    conn, "cv-industry-energy", ["x"], expected_version=version, now=LATER
                )
        with db.transaction() as conn:
            assert not repo().set_aliases(
                conn, "no-such-value", ["x"], expected_version=1, now=LATER
            )
        assert table(db) == before
    finally:
        db.close()


def test_set_aliases_is_atomic_when_two_writers_hold_the_same_version(tmp_path: Path) -> None:
    db = fresh(tmp_path)
    try:
        with db.transaction() as conn:
            first = repo().set_aliases(
                conn, "cv-seniority-lead", ["one"], expected_version=1, now=LATER
            )
        with db.transaction() as conn:
            second = repo().set_aliases(
                conn, "cv-seniority-lead", ["two"], expected_version=1, now=LATER
            )
        assert (first, second) == (True, False)
        found = row(db, "cv-seniority-lead")
        assert found is not None and json.loads(found["aliases"]) == ["one"]
        assert found["version"] == 2
    finally:
        db.close()


def test_set_aliases_refuses_work_mode_and_leaves_it_alone(tmp_path: Path) -> None:
    db = fresh(tmp_path)
    try:
        before = table(db)
        with pytest.raises(ValueError), db.transaction() as conn:
            repo().set_aliases(conn, "cv-work_mode-remote", ["x"], expected_version=1, now=LATER)
        assert table(db) == before
    finally:
        db.close()


# --- delete only when unused -----------------------------------------------------------------


def test_delete_unused_removes_a_value_no_job_holds(tmp_path: Path) -> None:
    db = fresh(tmp_path)
    try:
        hold(db, "industry", "energy", 2)  # another value is in use; this one is not
        with db.transaction() as conn:
            assert repo().delete_unused(conn, "cv-industry-retail", expected_version=1) == 0
        assert row(db, "cv-industry-retail") is None
        assert row(db, "cv-industry-energy") is not None
    finally:
        db.close()


def test_delete_unused_keeps_a_value_that_jobs_hold_and_says_how_many(tmp_path: Path) -> None:
    db = fresh(tmp_path)
    try:
        hold(db, "industry", "energy", 3)
        before = table(db)
        with db.transaction() as conn:
            assert repo().delete_unused(conn, "cv-industry-energy", expected_version=1) == 3
        assert table(db) == before
    finally:
        db.close()


def test_a_job_counts_whatever_its_state_or_whether_the_field_was_corrected(
    tmp_path: Path,
) -> None:
    db = fresh(tmp_path)
    try:
        hold(db, "seniority", "lead", 2, corrected=True)
        with db.transaction() as conn:
            conn.execute("UPDATE jobs SET triage_state='removed', removal_reason='other'")
            assert repo().jobs_holding(conn, "seniority", "lead") == 2
            assert repo().delete_unused(conn, "cv-seniority-lead", expected_version=1) == 2
        assert row(db, "cv-seniority-lead") is not None
    finally:
        db.close()


def test_a_value_held_in_another_field_does_not_block_this_one(tmp_path: Path) -> None:
    db = fresh(tmp_path)
    try:
        hold(db, "seniority", "energy", 2)  # same text, other column
        with db.transaction() as conn:
            assert repo().jobs_holding(conn, "industry", "energy") == 0
            assert repo().delete_unused(conn, "cv-industry-energy", expected_version=1) == 0
        assert row(db, "cv-industry-energy") is None
    finally:
        db.close()


def test_delete_unused_checks_the_version_before_the_use_count(tmp_path: Path) -> None:
    db = fresh(tmp_path)
    try:
        hold(db, "industry", "energy", 1)
        before = table(db)
        for value_id, version in (("cv-industry-energy", 5), ("cv-industry-retail", 5)):
            with pytest.raises(VersionConflict), db.transaction() as conn:
                repo().delete_unused(conn, value_id, expected_version=version)
        with pytest.raises(LookupError), db.transaction() as conn:
            repo().delete_unused(conn, "no-such-value", expected_version=1)
        assert table(db) == before
    finally:
        db.close()


def test_delete_unused_is_atomic_with_a_job_arriving_after_the_check(tmp_path: Path) -> None:
    """The use check and the delete are one statement: a job that holds the value when the
    delete runs keeps it, even if the caller read the value as unused a moment earlier."""
    db = fresh(tmp_path)
    try:
        with db.read() as conn:
            assert repo().jobs_holding(conn, "industry", "retail") == 0  # the page's view
        hold(db, "industry", "retail", 1)  # a job takes it before the delete
        with db.transaction() as conn:
            assert repo().delete_unused(conn, "cv-industry-retail", expected_version=1) == 1
        assert row(db, "cv-industry-retail") is not None
    finally:
        db.close()


def test_delete_unused_refuses_work_mode_and_leaves_it_alone(tmp_path: Path) -> None:
    db = fresh(tmp_path)
    try:
        before = table(db)
        with pytest.raises(ValueError), db.transaction() as conn:
            repo().delete_unused(conn, "cv-work_mode-hybrid", expected_version=1)
        assert table(db) == before
    finally:
        db.close()


# --- the listing -----------------------------------------------------------------------------


def test_values_with_use_lists_a_field_by_value_with_aliases_version_and_job_count(
    tmp_path: Path,
) -> None:
    db = fresh(tmp_path)
    try:
        hold(db, "employment_type", "contract", 2)
        with db.read() as conn:
            listed = repo().values_with_use(conn, "employment_type")
        assert [item["value"] for item in listed] == sorted(item["value"] for item in listed)
        assert {item["value"] for item in listed} == {
            "contract",
            "freelance",
            "full_time",
            "internship",
            "part_time",
            "temporary",
        }
        by_value = {item["value"]: item for item in listed}
        assert by_value["contract"]["jobs"] == 2 and by_value["freelance"]["jobs"] == 0
        assert by_value["contract"]["aliases"] == ["contractor", "fixed-term", "fixed term"]
        assert by_value["contract"]["id"] == "cv-employment_type-contract"
        assert by_value["contract"]["version"] == 1
        assert {item["field"] for item in listed} == {"employment_type"}
        with db.read() as conn:
            work = repo().values_with_use(conn, "work_mode")  # read only on the page, listed
        assert [item["value"] for item in work] == ["hybrid", "on_site", "remote"]
    finally:
        db.close()
