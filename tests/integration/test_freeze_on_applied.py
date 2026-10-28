"""M6.10 the freeze: the first move into an active status writes the submission and marks the
versions that were sent, in the same transaction as ``applied_at`` (AC-M6.7; ADR-102 item 8;
ADR-106 item 10; ADR-110 consequences). Per document the latest APPROVED version that has an
export is frozen, else the latest approved one, else none; a draft is never sent; later edits make
new versions and the sent ones and their files never change.

Assumed interface: the docstring of ``tests/export_pages.py`` ("The freeze"). The move goes through
the existing ``POST /applications/{id}/status`` route (board, menu, drawer) and the Submit step's
``POST .../tailoring/5/applied``. Real SQLite in a temp folder, a frozen clock, no network.
"""

from datetime import datetime, timedelta

import pytest

from tests.export_pages import Exporting, exporting_app
from tests.integration.test_board import snapshot
from tests.tailoring_ai_inputs import FACT_1, KEPT_TEXT

pytestmark = pytest.mark.integration

FIRST_LINE = "Planned 60 delivery routes a week with a team of 3."
SOURCE = {"board": "board", "menu": "menu", "drawer": "drawer", "submit": "drawer"}


@pytest.fixture
def d(tmp_path):
    with exporting_app(tmp_path) as desk:
        yield desk


def exported(d: Exporting, job_id: str = "freeze-job", **options):
    app = d.export_ready(job_id, **options)
    d.export_ok(app.id)
    return app


def versions_of(d: Exporting, aid: str) -> tuple[str, str]:
    return d.current_version_id(aid), d.current_letter_id(aid)


def same_instant(left: str, right: str) -> bool:
    return datetime.fromisoformat(left) == datetime.fromisoformat(right)


def reword(d: Exporting, aid: str, text: str = KEPT_TEXT, *, approve: bool = True) -> str:
    """The CV's first line reworded: a new draft version, approved unless ``approve`` is False."""
    current = d.current_version_id(aid)
    unit = d.units(current, removed=False)[1]
    new = d.service.edit_unit(aid, current, unit["id"], text).version_id
    if approve:
        d.service.approve(aid, new)
    return new


# --- the first active move freezes ---------------------------------------------------------------


@pytest.mark.parametrize("how", ["board", "menu", "drawer", "submit"])
def test_ac_m6_7_first_active_move_freezes(d: Exporting, how: str):
    app = exported(d)
    cv, letter = versions_of(d, app.id)
    assert d.submissions() == [] and d.application_row(app.id)["applied_at"] is None
    exports_before = d.exports(app.id)
    files_before = d.folder_state(app.id)

    answer = d.mark_applied(app.id) if how == "submit" else d.move(app.id, "applied", how)

    assert answer.status_code == 200, (answer.status_code, answer.text[:300])
    row = d.application_row(app.id)
    assert row["status_key"] == "applied" and row["applied_at"] is not None
    (submission,) = d.submissions()
    assert submission["application_id"] == app.id
    assert (submission["cv_version_id"], submission["letter_version_id"]) == (cv, letter)
    assert same_instant(submission["frozen_at"], row["applied_at"])
    assert d.status_of_version(cv) == d.status_of_version(letter) == "sent"
    assert d.history_sources(app.id)[-1] == SOURCE[how]
    # The files and their rows are what was exported; freezing renders and writes nothing.
    assert d.exports(app.id) == exports_before and d.folder_state(app.id) == files_before
    assert d.service.view(app.id).version.status == "sent"
    assert d.letters.view(app.id).version.status == "sent"


def test_freeze_without_a_letter_names_only_the_cv(d: Exporting):
    app = exported(d, letter=False)
    cv = d.current_version_id(app.id)

    d.move_ok(app.id, "applied")

    (submission,) = d.submissions()
    assert (submission["cv_version_id"], submission["letter_version_id"]) == (cv, None)
    assert d.status_of_version(cv) == "sent" and d.letter_document(app.id) is None


def test_applied_without_documents_records_none(d: Exporting):
    app = d.cv_ready("no-documents-job", template=False)
    before = d.all_tables()

    answer = d.move_ok(app.id, "applied")

    assert answer.status_code == 200
    row = d.application_row(app.id)
    (submission,) = d.submissions()
    assert (submission["cv_version_id"], submission["letter_version_id"]) == (None, None)
    assert same_instant(submission["frozen_at"], row["applied_at"])
    after = d.all_tables()
    for table in ("documents", "document_versions", "document_units", "document_exports"):
        assert after[table] == before[table] == []
    # A document that was only ever a draft is not sent either.
    other = d.export_ready("draft-only-job", cv_approved=False, letter_approved=False)
    cv, letter = versions_of(d, other.id)
    d.move_ok(other.id, "applied")
    mine = [s for s in d.submissions() if s["application_id"] == other.id]
    assert [(s["cv_version_id"], s["letter_version_id"]) for s in mine] == [(None, None)]
    assert d.status_of_version(cv) == d.status_of_version(letter) == "draft"


def test_freeze_prefers_the_exported_approved_version_then_the_latest_approved(d: Exporting):
    # The CV was exported as version 1; version 2 is approved but was never exported. The letter
    # was approved after the export, so it has no export at all: the latest approved is frozen.
    app = d.export_ready("prefer-job", letter_approved=False)
    d.export_ok(app.id)
    cv_exported = d.current_version_id(app.id)
    letter_v1 = d.current_letter_id(app.id)
    d.letters.approve(app.id, letter_v1)
    cv_later = reword(d, app.id)
    assert d.status_of_version(cv_later) == "approved" and cv_later != cv_exported
    other = "Planned 60 delivery routes a week, with a team of 3."
    draft_after = reword(d, app.id, other, approve=False)  # and a newer draft on top of it

    d.move_ok(app.id, "applied")

    (submission,) = d.submissions()
    assert submission["cv_version_id"] == cv_exported  # the files the user can have sent
    assert submission["letter_version_id"] == letter_v1
    assert d.status_of_version(cv_exported) == "sent"
    assert d.status_of_version(cv_later) == "approved"  # approved, but not what was exported
    assert d.status_of_version(draft_after) == "draft"
    assert d.status_of_version(letter_v1) == "sent"

    # With no export at all the latest approved version is the one frozen.
    plain = d.export_ready("plain-job", letter=False)
    first = d.current_version_id(plain.id)
    second = reword(d, plain.id)
    d.move_ok(plain.id, "applied")
    mine = next(s for s in d.submissions() if s["application_id"] == plain.id)
    assert mine["cv_version_id"] == second and d.status_of_version(first) == "approved"


def test_a_closed_move_from_preparing_does_not_freeze_until_the_first_active_move(d: Exporting):
    app = exported(d)
    cv, letter = versions_of(d, app.id)

    d.move_ok(app.id, "withdrawn")

    assert d.application_row(app.id)["applied_at"] is None and d.submissions() == []
    assert d.status_of_version(cv) == d.status_of_version(letter) == "approved"
    d.move_ok(app.id, "applied")
    assert [s["application_id"] for s in d.submissions()] == [app.id]
    assert d.status_of_version(cv) == d.status_of_version(letter) == "sent"


def test_freeze_failure_rolls_back_the_move(d: Exporting):
    app = exported(d)
    cv, letter = versions_of(d, app.id)
    d.r.run.execute(
        "CREATE TRIGGER freeze_boom BEFORE INSERT ON application_submissions "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    before = d.all_tables()

    answer = d.move(app.id, "applied", "menu")

    assert answer.status_code >= 400
    assert d.all_tables() == before  # no status, history row, applied_at or sent version
    assert d.application_row(app.id)["applied_at"] is None
    assert d.status_of_version(cv) == d.status_of_version(letter) == "approved"
    d.r.run.execute("DROP TRIGGER freeze_boom")
    d.move_ok(app.id, "applied")  # and it works once the cause is gone
    assert len(d.submissions()) == 1


# --- after the freeze ----------------------------------------------------------------------------


def test_ac_m6_7_edits_after_freeze_make_new_versions_sent_unchanged(d: Exporting):
    app = exported(d)
    cv, letter = versions_of(d, app.id)
    d.move_ok(app.id, "applied")
    sent_state = {
        "versions": [d.version_row(v) for v in (cv, letter)],
        "units": [d.units(v) for v in (cv, letter)],
        "sent_facts": [d.sent_pairs(v) for v in (cv, letter)],
        "exports": d.exports(app.id),
        "files": d.folder_state(app.id),
        "submissions": d.submissions(),
    }

    # Editing the sent CV and the sent letter makes new drafts; the sent ones stay as they were.
    unit = d.units(cv, removed=False)[1]
    new_cv = d.service.edit_unit(app.id, cv, unit["id"], KEPT_TEXT).version_id
    paragraph = d.units(letter, removed=False)[0]
    new_letter = d.letters.edit_unit(app.id, letter, paragraph["id"], FACT_1).version_id

    for new, old in ((new_cv, cv), (new_letter, letter)):
        row = d.version_row(new)
        assert new != old and row["status"] == "draft" and row["parent_version_id"] == old
        assert row["number"] == d.version_row(old)["number"] + 1
    assert d.current_version_id(app.id) == new_cv and d.current_letter_id(app.id) == new_letter
    assert KEPT_TEXT in d.texts(new_cv) and KEPT_TEXT not in d.texts(cv)

    # Approving and exporting the new versions adds files; it never rewrites the sent ones.
    d.service.approve(app.id, new_cv)
    d.letters.approve(app.id, new_letter)
    d.export_ok(app.id)
    assert len(d.exports(app.id)) == 8

    assert [d.version_row(v) for v in (cv, letter)] == sent_state["versions"]
    assert [d.units(v) for v in (cv, letter)] == sent_state["units"]
    assert [d.sent_pairs(v) for v in (cv, letter)] == sent_state["sent_facts"]
    assert d.submissions() == sent_state["submissions"]
    kept = sent_state["exports"]
    assert [row for row in d.exports(app.id) if row in kept] == kept
    old_files = {row["stored_name"] for row in kept}
    assert {k: v for k, v in d.folder_state(app.id).items() if k in old_files} == (
        sent_state["files"]
    )
    # The new versions are approved, not sent: nothing was frozen a second time.
    assert d.status_of_version(new_cv) == d.status_of_version(new_letter) == "approved"


def test_ac_m6_7_fact_edit_after_freeze_keeps_sent_fact_version(d: Exporting):
    from app.services.facts import FactInput

    app = exported(d)
    cv, _letter = versions_of(d, app.id)
    cited_before = [(row["text"], row["cited"]) for row in d.units(cv)]
    sent_before = d.sent_pairs(cv)
    files_before = d.folder_state(app.id)
    assert (1, 1) in sent_before and any("F1@1" in cited for _text, cited in cited_before)
    d.move_ok(app.id, "applied")

    one = d.r.facts.get(d.fact_row(1)["id"])
    d.r.facts.edit(
        one.id,
        FactInput(text="Planned 75 delivery routes a week with a team of 5.", role_id=one.role_id),
        expected_version=one.version,
    )

    assert d.fact_row(1)["id"] == one.id
    assert d.r.facts.get(one.id).version == one.version + 1  # the fact has a newer version
    assert [(row["text"], row["cited"]) for row in d.units(cv)] == cited_before
    assert d.sent_pairs(cv) == sent_before  # the sent version still names version 1 of F1
    assert d.status_of_version(cv) == "sent"
    shown = d.service.view(app.id, cv)
    wordings = [cite.text for unit in shown.units for cite in unit.cited]
    assert (
        FACT_1 in wordings and "Planned 75 delivery routes a week with a team of 5." not in wordings
    )
    assert d.folder_state(app.id) == files_before  # the files were not rendered again


def test_freeze_once_after_round_trip_to_preparing(d: Exporting):
    app = exported(d)
    cv, letter = versions_of(d, app.id)
    d.move_ok(app.id, "applied")
    first_row = d.application_row(app.id)
    (submission,) = d.submissions()

    # Later: new documents are approved and exported, then the application goes back and forth.
    d.r.run.clock.advance(timedelta(days=2))
    newer = reword(d, app.id)
    d.export_ok(app.id)
    d.move_ok(app.id, "preparing")
    d.r.run.clock.advance(timedelta(days=1))
    d.move_ok(app.id, "applied")
    d.move_ok(app.id, "interviewing", "board")

    assert d.submissions() == [submission]  # one row, as it was
    assert d.application_row(app.id)["applied_at"] == first_row["applied_at"]
    assert d.status_of_version(cv) == d.status_of_version(letter) == "sent"
    assert d.status_of_version(newer) == "approved"  # not frozen by the second Applied
    assert len(d.history_sources(app.id)) == 5
    assert d.version_row(cv)["status"] == "sent"
    # The Submit step's own button is gone once the application has been applied.
    assert d.mark_applied(app.id).status_code == 409
    assert d.submissions() == [submission] and d.status_of_version(newer) == "approved"


def test_renderer_unavailable_word_only_export_is_what_gets_frozen(tmp_path):
    from tests.export_pages import UnavailablePdf, real_renderers

    with exporting_app(
        tmp_path, renderers=lambda clock: (UnavailablePdf(), real_renderers(clock)[1])
    ) as d:
        app = d.export_ready()
        later = d.export(app.id)
        assert later.status_code == 200
        cv = d.current_version_id(app.id)
        newer = reword(d, app.id)  # approved, not exported

        d.move_ok(app.id, "applied")

        (submission,) = d.submissions()
        assert submission["cv_version_id"] == cv  # the version that has a (Word) export
        assert d.status_of_version(newer) == "approved"


def test_reading_the_submit_step_after_applied_writes_nothing(d: Exporting):
    app = exported(d)
    d.move_ok(app.id, "applied")
    before = snapshot(d.r)

    assert d.step5(app.id).status_code == 200 and d.page5(app.id).status_code == 200

    assert snapshot(d.r) == before
