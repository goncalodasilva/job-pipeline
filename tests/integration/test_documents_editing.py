"""M6.7 service half: resolving flags, regenerating, editing and approving a tailored CV through
``DocumentService`` (AC-M6.1, AC-M6.3, AC-M6.4; spec 3.1 items 4 and 6, 3.2; ADR-106 items 8, 9;
ADR-110 items 2 and 8).

Assumed interface: see the docstring of ``tests/document_pages.py`` (``edit_unit``, ``add_unit``,
``remove_unit``, ``resolve_flag_confirm``, ``approve``, ``view``, ``start_generate`` with
``replace_all`` and ``confirm_replace``). Every edit, addition, removal and confirmation writes a
NEW draft version (version checked, parent set); an approved version is never touched. Real SQLite
in a temp folder, the fake provider behind the spy, no network, no fixed waits.
"""

from typing import Any

import pytest

from app.domain.errors import VersionConflict
from app.domain.facts import Privacy
from tests.document_pages import (
    LANGUAGE_UNIT,
    NOTE,
    ROLE_A,
    SAVE_AS_FACT,
    SUMMARY_UNIT,
    TAILOR_CV,
    Documents,
    documents_app,
)
from tests.integration.test_documents_generation import (
    GLOBEX,
    INFLATED,
    OK_TEXTS,
    UNCITED,
)
from tests.tailoring_ai_inputs import FACT_1, FACT_2, FACT_7, KEPT_TEXT, ROLE

pytestmark = pytest.mark.integration


@pytest.fixture
def d(tmp_path):
    with documents_app(tmp_path) as desk:
        yield desk


def generated(d: Documents, fixture: str = "ok", job: str = "cv-job") -> tuple[Any, str]:
    app = d.cv_ready(job)
    d.generate(app.id, fixture)
    return app, d.current_version_id(app.id)


def flagged_ids(d: Documents, version_id: str) -> set[str]:
    return {row["id"] for row in d.units(version_id, removed=False) if row["flag_list"]}


# --- AC-M6.1: a flag is resolved by edit, remove or confirm -----------------------------------


@pytest.mark.parametrize("how", ["edit", "remove", "confirm"])
def test_ac_m6_1_approval_blocked_until_each_flag_resolved(d: Documents, how: str):
    app, v1 = generated(d, "adversarial_invented_employer")  # two flagged lines
    service = d.service
    with pytest.raises(d.docs.ApprovalBlocked) as blocked:
        service.approve(app.id, v1)
    assert blocked.value.message == "2 lines need your check."
    assert {item.unit_id for item in blocked.value.blockers} == flagged_ids(d, v1)
    first = d.unit_with(v1, GLOBEX)

    if how == "edit":
        v2 = service.edit_unit(app.id, v1, first["id"], FACT_1).version_id
    elif how == "remove":
        v2 = service.remove_unit(app.id, v1, first["id"])
    else:
        v2 = service.resolve_flag_confirm(app.id, v1, first["id"]).version_id

    assert v2 != v1 and d.current_version_id(app.id) == v2
    assert len(flagged_ids(d, v2)) == 1  # the other line (its invented role) is still flagged
    with pytest.raises(d.docs.ApprovalBlocked) as still:
        service.approve(app.id, v2)
    assert still.value.message == "1 line needs your check."
    (second,) = flagged_ids(d, v2)
    v3 = service.remove_unit(app.id, v2, second)

    assert service.view(app.id).blockers == ()
    service.approve(app.id, v3)
    assert d.version_row(v3)["status"] == "approved"
    # The versions that were resolved from are untouched drafts that still carry their flags.
    assert d.version_row(v1)["status"] == "draft" and len(flagged_ids(d, v1)) == 2
    assert d.version_row(v2)["status"] == "draft"


def test_ac_m6_1_confirm_creates_confirmed_fact_and_unit_cites_it(d: Documents):
    app, v1 = generated(d, "adversarial_inflated_number")
    flagged = d.unit_with(v1, INFLATED)
    before = d.table_counts()

    # One transaction: when the new version can't be written, no fact is left behind.
    d.r.run.execute(
        "CREATE TRIGGER fictional_abort BEFORE INSERT ON document_versions WHEN NEW.number = 2 "
        "BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
    )
    with pytest.raises(Exception):  # noqa: B017 - the storage error, whatever its wrapper
        d.service.resolve_flag_confirm(app.id, v1, flagged["id"])
    assert d.table_counts() == before and d.current_version_id(app.id) == v1
    d.r.run.execute("DROP TRIGGER fictional_abort")

    outcome = d.service.resolve_flag_confirm(app.id, v1, flagged["id"])

    after = d.table_counts()
    assert after["facts"] == before["facts"] + 1
    assert after["fact_versions"] == before["fact_versions"] + 1
    assert after["document_versions"] == before["document_versions"] + 1
    fact = d.fact_row(9)
    assert fact["status"] == "confirmed" and fact["privacy"] == "normal"
    assert fact["role_id"] == d.role_id(ROLE_A[0])  # the role of the line's section
    (wording,) = d.rows("SELECT * FROM fact_versions WHERE fact_id=?", (fact["id"],))
    assert wording["version"] == 1 and wording["text"] == INFLATED
    assert wording["source"] == "document"

    v2 = d.version_row(outcome.version_id)
    assert (v2["status"], v2["parent_version_id"], v2["origin"]) == ("draft", v1, "user")
    assert d.current_version_id(app.id) == v2["id"] and outcome.offer is None
    unit = d.unit_with(v2["id"], INFLATED)
    assert unit["id"] == outcome.unit_id and unit["origin"] == "user"
    assert unit["cited"] == ["F9@1"] and unit["flag_list"] == []
    assert unit["role_title"] == ROLE_A[0]
    assert d.texts(v2["id"]) == d.texts(v1)  # nothing else changed
    assert d.service.view(app.id).blockers == ()
    # The version it came from still has its flags and no new citation.
    assert d.unit_with(v1, INFLATED)["flag_list"] and d.version_row(v1)["status"] == "draft"
    d.service.approve(app.id, v2["id"])

    # A line outside any role gives a fact without a role.
    other = d.cv_ready("second-cv-job")
    d.generate(other.id, "adversarial_uncited_paragraph")
    version = d.current_version_id(other.id)
    done = d.service.resolve_flag_confirm(other.id, version, d.unit_with(version, UNCITED)["id"])
    fact = d.fact_row(10)
    assert fact["status"] == "confirmed" and fact["role_id"] is None
    (wording,) = d.rows("SELECT * FROM fact_versions WHERE fact_id=?", (fact["id"],))
    assert wording["text"] == UNCITED and wording["source"] == "document"
    assert d.unit_with(done.version_id, UNCITED)["cited"] == ["F10@1"]


# --- AC-M6.3: regenerate keeps edits unless everything is replaced ---------------------------


def test_ac_m6_3_regenerate_keeps_user_edits_and_removals(d: Documents):
    app, v1 = generated(d)
    v2 = d.service.edit_unit(app.id, v1, d.unit_with(v1, FACT_1)["id"], KEPT_TEXT).version_id
    v3 = d.service.remove_unit(app.id, v2, d.unit_with(v2, FACT_7)["id"])
    before = {version: d.units(version) for version in (v1, v2, v3)}

    d.generate(app.id, "ok", note=NOTE)

    v4 = d.current_version_id(app.id)
    row = d.version_row(v4)
    assert (row["number"], row["parent_version_id"], row["status"]) == (4, v3, "draft")
    assert row["origin"] == "ai" and row["note"] == NOTE
    visible = d.units(v4, removed=False)
    # The edit stays where it stood; the other slots take the new lines; the removed line stays out.
    assert [unit["text"] for unit in visible] == [SUMMARY_UNIT, KEPT_TEXT, FACT_2, LANGUAGE_UNIT]
    kept = visible[1]
    assert kept["origin"] == "user" and kept["cited"] == ["F1@1"]
    assert FACT_1 not in d.texts(v4, removed=True)  # the original wording is not brought back
    assert [unit["text"] for unit in d.units(v4) if unit["removed"]] == [FACT_7]  # the tombstone
    # The request held the kept wording and the note as data; earlier versions are untouched.
    request = d.requests(TAILOR_CV)[-1]
    assert KEPT_TEXT in request.user and NOTE in request.user
    assert {version: d.units(version) for version in (v1, v2, v3)} == before
    view = d.service.view(app.id)
    assert [item.number for item in view.versions] == [4, 3, 2, 1]
    assert view.version.parent_version_id == v3


def test_ac_m6_3_replace_everything_needs_confirmation(d: Documents):
    app, v1 = generated(d)
    v2 = d.service.edit_unit(app.id, v1, d.unit_with(v1, FACT_1)["id"], KEPT_TEXT).version_id
    tasks, requests = d.rows("SELECT * FROM tasks"), len(d.requests(TAILOR_CV))

    for options in ({"replace_all": True}, {"replace_all": True, "confirm_replace": False}):
        with pytest.raises(d.docs.ReplaceNotConfirmed) as refused:
            d.service.start_generate(app.id, **options)
        assert refused.value.message and "Traceback" not in refused.value.message
    assert d.rows("SELECT * FROM tasks") == tasks and len(d.requests(TAILOR_CV)) == requests
    assert d.current_version_id(app.id) == v2

    d.generate(app.id, "ok", replace_all=True, confirm_replace=True)

    v3 = d.current_version_id(app.id)
    assert d.version_row(v3)["parent_version_id"] == v2
    assert d.texts(v3, removed=True) == OK_TEXTS  # the edit is gone, and so are tombstones
    assert {unit["origin"] for unit in d.units(v3)} == {"ai"}
    assert KEPT_TEXT not in d.requests(TAILOR_CV)[-1].user  # nothing of it was sent
    assert KEPT_TEXT in d.texts(v2)  # the earlier version keeps it


# --- AC-M6.4: a new claim typed into the document ---------------------------------------------


def test_ac_m6_4_edit_with_new_claim_offers_save_as_fact(d: Documents):
    app, v1 = generated(d)
    text = "Planned 60 delivery routes a week with a team of 9 and a budget of $2m."
    unit = d.unit_with(v1, FACT_1)

    outcome = d.service.edit_unit(app.id, v1, unit["id"], text)

    assert outcome.offer is not None
    assert outcome.offer.question == SAVE_AS_FACT and outcome.offer.unit_id == outcome.unit_id
    assert outcome.offer.claims == ("9", "$2m")
    edited = d.unit_with(outcome.version_id, text)
    assert edited["id"] == outcome.unit_id and edited["origin"] == "user"
    assert edited["cited"] == ["F1@1"]  # an edit keeps the citations
    assert [(f["kind"], f["claim"]) for f in edited["flag_list"]] == [
        ("needs_check", "9"),
        ("needs_check", "$2m"),
    ]
    # Declining leaves the line flagged and the version unapprovable.
    with pytest.raises(d.docs.ApprovalBlocked):
        d.service.approve(app.id, outcome.version_id)

    # Answering yes saves the line as a confirmed fact the line then cites.
    facts = d.table_counts()["facts"]
    done = d.service.resolve_flag_confirm(app.id, outcome.version_id, outcome.unit_id)
    assert d.table_counts()["facts"] == facts + 1
    assert d.fact_row(9)["status"] == "confirmed"
    saved = d.unit_with(done.version_id, text)
    assert saved["cited"] == ["F9@1"] and saved["flag_list"] == []
    d.service.approve(app.id, done.version_id)

    # A line added with a new claim offers the same question.
    other, first = generated(d, "ok", "second-cv-job")
    added = d.service.add_unit(
        other.id,
        first,
        "experience",
        "Mentored 5 new planners on route software.",
        role=ROLE,
        fact_ids=("F1@1",),
    )
    assert added.offer is not None and added.offer.question == SAVE_AS_FACT
    assert added.offer.claims == ("5",)


def test_ac_m6_4_edit_without_new_claim_offers_nothing(d: Documents):
    app, v1 = generated(d)
    text = "Planned 60 delivery routes a week, with a team of 3."
    facts = d.table_counts()["facts"]

    outcome = d.service.edit_unit(app.id, v1, d.unit_with(v1, FACT_1)["id"], text)

    assert outcome.offer is None
    edited = d.unit_with(outcome.version_id, text)
    assert edited["origin"] == "user" and edited["flag_list"] == [] and edited["cited"] == ["F1@1"]
    assert d.table_counts()["facts"] == facts  # nothing was saved as a fact
    assert d.service.view(app.id).blockers == ()
    d.service.approve(app.id, outcome.version_id)

    added = d.service.add_unit(
        app.id, d.current_version_id(app.id), "languages", "Fluent English.", fact_ids=("F3@1",)
    )
    assert added.offer is None


# --- approval looks at the facts as they are now ----------------------------------------------


def test_approval_blocked_after_cited_fact_becomes_never_use_or_changes(d: Documents):
    from app.services.facts import FactInput

    app, v1 = generated(d)
    facts = d.r.facts
    assert d.service.view(app.id).blockers == ()  # clean when generated

    # F2 gets a new wording: the lines citing F2@1 cite an older version.
    two = facts.get(d.fact_row(2)["id"])
    facts.edit(
        two.id,
        FactInput(
            text="Cut invoice processing time by 40% across 4 warehouses.", role_id=two.role_id
        ),
        expected_version=two.version,
    )
    with pytest.raises(d.docs.ApprovalBlocked) as changed:
        d.service.approve(app.id, v1)
    assert changed.value.message == "2 lines need your check."  # the summary and the F2 line
    assert {(item.kind, item.claim) for item in changed.value.blockers} == {("needs_check", "F2@1")}

    # F1 becomes never-use: the lines citing it can no longer be approved.
    one = facts.get(d.fact_row(1)["id"])
    facts.set_privacy(one.id, Privacy.NEVER_USE, expected_version=one.version)
    with pytest.raises(d.docs.ApprovalBlocked) as hidden:
        d.service.approve(app.id, v1)
    assert hidden.value.message == "3 lines need your check."
    kinds = {(item.kind, item.claim) for item in hidden.value.blockers}
    assert kinds == {("needs_check", "F2@1"), ("unsupported", "F1@1")}
    assert d.version_row(v1)["status"] == "draft" and d.flag_triples(v1) == []  # nothing stored
    assert {item.kind for item in d.service.view(app.id).blockers} == {"needs_check", "unsupported"}

    # Removing the three lines that cite them clears the way, checked against the facts as now.
    version = v1
    for text in (SUMMARY_UNIT, FACT_1, FACT_2):
        version = d.service.remove_unit(app.id, version, d.unit_with(version, text)["id"])
    assert d.service.view(app.id).blockers == ()
    d.service.approve(app.id, version)
    assert d.version_row(version)["status"] == "approved"


# --- versions: every change is a new draft, stale ones conflict -------------------------------


def test_document_edit_add_remove_each_make_a_new_version(d: Documents):
    app, v1 = generated(d)

    v2 = d.service.edit_unit(app.id, v1, d.unit_with(v1, FACT_1)["id"], KEPT_TEXT).version_id
    added = "Mentored new planners on route software."
    v3 = d.service.add_unit(
        app.id, v2, "experience", added, role=ROLE, fact_ids=("F1@1",)
    ).version_id
    v4 = d.service.remove_unit(app.id, v3, d.unit_with(v3, FACT_2)["id"])

    rows = d.versions(app.id)
    assert [(row["number"], row["id"]) for row in rows] == [(1, v1), (2, v2), (3, v3), (4, v4)]
    assert [row["parent_version_id"] for row in rows] == [None, v1, v2, v3]
    assert [row["status"] for row in rows] == ["draft"] * 4
    assert [row["origin"] for row in rows] == ["ai", "user", "user", "user"]
    assert d.current_version_id(app.id) == v4
    # The added line sits after the last line of its section; positions stay contiguous.
    assert d.texts(v3) == [SUMMARY_UNIT, KEPT_TEXT, FACT_2, FACT_7, added, LANGUAGE_UNIT]
    new = d.unit_with(v3, added)
    assert (new["origin"], new["kind"], new["section"]) == ("user", "content", "experience")
    assert new["role_title"] == ROLE_A[0] and new["cited"] == ["F1@1"] and new["flag_list"] == []
    # The removed line is a tombstone: stored, hidden, ignored by the blockers.
    assert [unit["position"] for unit in d.units(v4)] == [0, 1, 2, 3, 4, 5]
    assert [unit["text"] for unit in d.units(v4) if unit["removed"]] == [FACT_2]
    assert FACT_2 not in [unit.text for unit in d.service.view(app.id).units]
    # The snapshot and the sent set follow every version; earlier versions never change.
    assert {row["template_snapshot"] for row in rows} == {rows[0]["template_snapshot"]}
    assert {frozenset(d.sent_pairs(row["id"])) for row in rows} == {frozenset(d.sent_pairs(v1))}
    assert d.texts(v1) == OK_TEXTS


def test_document_edit_stale_version_conflicts(d: Documents):
    app, v1 = generated(d)
    unit = d.units(v1)[1]
    v2 = d.service.edit_unit(app.id, v1, unit["id"], KEPT_TEXT).version_id
    counts, tables = d.table_counts(), d.cv_tables()
    service = d.service

    stale = [
        lambda: service.edit_unit(app.id, v1, unit["id"], "Planned routes every week."),
        lambda: service.add_unit(app.id, v1, "summary", "Plans routes.", fact_ids=("F1@1",)),
        lambda: service.remove_unit(app.id, v1, unit["id"]),
        lambda: service.resolve_flag_confirm(app.id, v1, unit["id"]),
        lambda: service.approve(app.id, v1),
    ]
    for attempt in stale:
        with pytest.raises(VersionConflict):
            attempt()
    assert d.table_counts() == counts and d.cv_tables() == tables
    assert d.current_version_id(app.id) == v2

    # The current version works, once; an approved version is not approved again.
    service.approve(app.id, v2)
    with pytest.raises(VersionConflict):
        service.approve(app.id, v2)
    assert d.version_row(v2)["status"] == "approved"
    # A regeneration moves the current version too: the one before it is stale.
    d.generate(app.id, "ok")
    with pytest.raises(VersionConflict):
        service.remove_unit(app.id, v2, d.units(v2)[0]["id"])


def test_editing_an_approved_version_makes_a_new_draft_and_approved_stays_approved(d: Documents):
    app, v1 = generated(d)
    d.service.approve(app.id, v1)
    approved_row, approved_units = d.version_row(v1), d.units(v1)

    outcome = d.service.edit_unit(app.id, v1, d.units(v1)[1]["id"], KEPT_TEXT)

    v2 = d.version_row(outcome.version_id)
    assert (v2["status"], v2["parent_version_id"], v2["number"]) == ("draft", v1, 2)
    assert v2["template_snapshot"] == approved_row["template_snapshot"]
    assert d.version_row(v1) == approved_row and d.units(v1) == approved_units  # frozen
    assert d.current_version_id(app.id) == v2["id"]
    assert d.sent_pairs(v2["id"]) == d.sent_pairs(v1)

    view = d.service.view(app.id)
    assert view.version.id == v2["id"] and view.version.status == "draft"
    assert [(item.number, item.status) for item in view.versions] == [(2, "draft"), (1, "approved")]
    old = d.service.view(app.id, v1)
    assert old.version.status == "approved" and [unit.text for unit in old.units] == OK_TEXTS
    d.service.approve(app.id, v2["id"])
    assert [row["status"] for row in d.versions(app.id)] == ["approved", "approved"]


def test_document_sent_facts_recorded_and_used_on_edit(d: Documents):
    app, v1 = generated(d)
    assert d.sent_pairs(v1) == {(1, 1), (2, 1), (3, 1), (7, 1), (8, 1)}  # F4 F5 F6 never sent
    unit = d.unit_with(v1, FACT_1)

    outside = d.service.edit_unit(app.id, v1, unit["id"], FACT_1, fact_ids=("F4@1",))

    # F4@1 is a real fact version, but it was not sent: the edit is checked against the sent set.
    flags = d.unit_with(outside.version_id, FACT_1)["flag_list"]
    assert ("unsupported", "F4@1") in [(f["kind"], f["claim"]) for f in flags]
    assert d.sent_pairs(outside.version_id) == d.sent_pairs(v1)
    with pytest.raises(d.docs.ApprovalBlocked):
        d.service.approve(app.id, outside.version_id)

    inside = d.service.edit_unit(
        app.id, outside.version_id, outside.unit_id, FACT_2, fact_ids=("F2@1",)
    )
    # The edited line now says what the F2 line says, so pick it by its id, not by its text.
    clean = next(u for u in d.units(inside.version_id) if u["id"] == inside.unit_id)
    assert clean["flag_list"] == [] and clean["cited"] == ["F2@1"]
    assert inside.offer is None


def test_document_edits_refuse_unusable_text_and_places(d: Documents):
    app, v1 = generated(d)
    unit = d.units(v1)[1]
    structure_app, structure = generated(d, "adversarial_structure_claim", "second-cv-job")
    heading = d.units(structure)[1]
    tables, service = d.cv_tables(), d.service

    refused = [
        lambda: service.edit_unit(app.id, v1, unit["id"], "   "),
        lambda: service.edit_unit(app.id, v1, unit["id"], "x" * 401),
        lambda: service.edit_unit(app.id, v1, unit["id"], "Planned\x07 routes."),
        lambda: service.add_unit(app.id, v1, "hobbies", "Chess.", fact_ids=("F1@1",)),
        lambda: service.add_unit(app.id, v1, "summary", "Plans.", role="Chief Dreamer at Nowhere"),
        lambda: service.resolve_flag_confirm(structure_app.id, structure, heading["id"]),
    ]
    for attempt in refused:
        with pytest.raises(d.docs.UnitInvalid) as error:
            attempt()
        assert error.value.message
    with pytest.raises(d.docs.UnitNotFound):
        service.remove_unit(app.id, v1, "no-such-unit")
    assert d.cv_tables() == tables
    assert len(d.versions(app.id)) == 1 and len(d.versions(structure_app.id)) == 1
    assert d.current_version_id(app.id) == v1
    assert d.table_counts()["facts"] == 8  # a refused confirm saved no fact


# --- review fixes: what a regeneration may send, caps, checks, citations -----------------------


def _letters(i: int) -> str:
    return chr(97 + i // 26) + chr(97 + i % 26)


def test_kept_line_citing_a_now_generalized_fact_is_not_sent(d: Documents):
    app, v1 = generated(d)
    text = "Mapped a weekly route plan for the whole depot."
    d.service.edit_unit(app.id, v1, d.unit_with(v1, FACT_1)["id"], text)
    one = d.r.facts.get(d.fact_row(1)["id"])
    d.r.facts.set_privacy(one.id, Privacy.GENERALIZE, expected_version=one.version)

    d.generate(app.id, "ok")

    assert text not in d.requests(TAILOR_CV)[-1].user
    assert text in d.texts(d.current_version_id(app.id))  # still in the CV


def test_kept_line_citing_a_superseded_fact_version_is_not_sent(d: Documents):
    from app.services.facts import FactInput

    app, v1 = generated(d)
    text = "Trimmed invoice delays across the depots."
    d.service.edit_unit(app.id, v1, d.unit_with(v1, FACT_2)["id"], text)
    two = d.r.facts.get(d.fact_row(2)["id"])
    d.r.facts.edit(
        two.id,
        FactInput(
            text="Cut invoice processing time by 40% across 4 warehouses.", role_id=two.role_id
        ),
        expected_version=two.version,
    )

    d.generate(app.id, "ok")

    assert text not in d.requests(TAILOR_CV)[-1].user
    assert text in d.texts(d.current_version_id(app.id))


def test_approve_refuses_a_version_with_no_visible_lines(d: Documents):
    app, version = generated(d)
    for text in OK_TEXTS:
        version = d.service.remove_unit(app.id, version, d.unit_with(version, text)["id"])
    assert d.service.view(app.id).units == ()

    with pytest.raises(d.docs.NothingToApprove) as error:
        d.service.approve(app.id, version)

    assert error.value.message == "There is nothing to approve yet."
    assert d.version_row(version)["status"] == "draft"


def test_edit_unit_added_details_none_keeps_the_existing_details(d: Documents):
    import json

    app, v1 = generated(d)
    added = d.service.add_unit(
        app.id,
        v1,
        "experience",
        "Planned routes weekly.",
        role=ROLE,
        fact_ids=("F1@1",),
        added_details=("weekly",),
    )

    def details(outcome: Any) -> list[str]:
        row = next(u for u in d.units(outcome.version_id) if u["id"] == outcome.unit_id)
        return json.loads(row["added_details"])

    assert details(added) == ["weekly"]
    kept = d.service.edit_unit(app.id, added.version_id, added.unit_id, "Planned routes often.")
    assert details(kept) == ["weekly"]
    replaced = d.service.edit_unit(
        app.id, kept.version_id, kept.unit_id, "Planned routes.", added_details=(" often ",)
    )
    assert details(replaced) == ["often"]
    cleared = d.service.edit_unit(
        app.id, replaced.version_id, replaced.unit_id, "Planned routes.", added_details=()
    )
    assert details(cleared) == []


def test_line_caps_120_per_version_and_80_kept_lines_sent(d: Documents):
    app, version = generated(d)
    service = d.service

    def add(index: int) -> Any:
        return service.add_unit(
            app.id,
            current,
            "experience",
            f"Added line {_letters(index)}.",
            role=ROLE,
            fact_ids=("F1@1",),
        )

    current = version
    for index in range(115):  # 5 lines + 115 = 120 visible lines
        current = add(index).version_id
    tables = d.cv_tables()
    with pytest.raises(d.docs.UnitInvalid) as full:
        add(115)
    assert (
        full.value.message == "A CV can have at most 120 lines. Remove one before adding another."
    )
    assert d.cv_tables() == tables and d.current_version_id(app.id) == current

    # A removed line no longer counts.
    current = service.remove_unit(app.id, current, d.unit_with(current, FACT_7)["id"])
    current = add(115).version_id

    d.generate(app.id, "ok")

    sent = d.requests(TAILOR_CV)[-1].user
    assert sent.count("Added line ") == 80
    assert f"Added line {_letters(36)}." in sent and f"Added line {_letters(35)}." not in sent
    assert f"Added line {_letters(115)}." in sent
    # Every kept line is still in the new version.
    texts = d.texts(d.current_version_id(app.id))
    assert all(f"Added line {_letters(i)}." in texts for i in range(116))


def test_user_references_and_details_are_checked_like_the_model_output(d: Documents):
    from app.domain.tailored_cv import check_added_details, check_fact_ids

    def message(check: Any, value: Any) -> str:
        with pytest.raises(ValueError) as error:
            check(value)
        return str(error.value)

    app, v1 = generated(d)
    unit = d.units(v1)[1]
    service, tables = d.service, d.cv_tables()
    bad_ids = [("not a reference",), ("F1@1\n",), tuple(f"F{i}@1" for i in range(1, 22))]
    bad_details = [("",), ("x" * 201,), ("a",) * 11, ("fast\u202etrack",), ("two\nlines",)]

    for ids in bad_ids:
        expected = message(check_fact_ids, ids)
        for attempt in (
            lambda ids=ids: service.edit_unit(app.id, v1, unit["id"], FACT_1, fact_ids=ids),
            lambda ids=ids: service.add_unit(app.id, v1, "summary", "Plans.", fact_ids=ids),
        ):
            with pytest.raises(d.docs.UnitInvalid) as error:
                attempt()
            assert error.value.message == expected
    for details in bad_details:
        expected = message(check_added_details, details)
        for attempt in (
            lambda dt=details: service.edit_unit(app.id, v1, unit["id"], FACT_1, added_details=dt),
            lambda dt=details: service.add_unit(
                app.id, v1, "summary", "Plans.", fact_ids=("F1@1",), added_details=dt
            ),
        ):
            with pytest.raises(d.docs.UnitInvalid) as error:
                attempt()
            assert error.value.message == expected
    assert d.cv_tables() == tables and d.current_version_id(app.id) == v1


def test_citations_are_stored_only_for_sent_or_document_saved_facts(d: Documents):
    from tests.tailoring_ai_inputs import HIDDEN

    app, v1 = generated(d)
    unit = d.unit_with(v1, FACT_1)

    outcome = d.service.edit_unit(app.id, v1, unit["id"], FACT_1, fact_ids=("F4@1", "F2@1"))

    row = next(u for u in d.units(outcome.version_id) if u["id"] == outcome.unit_id)
    assert row["cited"] == ["F2@1"]  # F4 is never-use and was not sent: no citation row
    assert ("unsupported", "F4@1") in [(f["kind"], f["claim"]) for f in row["flag_list"]]
    view = d.service.view(app.id)
    shown = next(u for u in view.units if u.unit_id == outcome.unit_id)
    assert [w.text for w in shown.cited] == [FACT_2] and HIDDEN not in repr(view)
    assert {(item.kind, item.claim) for item in view.blockers} >= {("unsupported", "F4@1")}
