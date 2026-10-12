"""Merging a draft into a fact, and confirming one import's drafts together (M3.3; AC-M3.2 at
service level; spec 3.5; ADR-090 items 3 and 5).

``FactService.merge(draft_id, target_id, content: FactContent, *, expected_draft_version,
expected_target_version) -> FactView`` (the target): ``content`` is the wording the user settled
on for the target (kept, taken from the draft or edited). In one transaction the target gets a
new version with source ``merge`` and the event ``merged``, its skills become the union of both
facts' skills, and the draft is closed as discarded with ``merged_into_fact_id`` set and the
event ``merged_into``. Only a draft can be merged, and never into itself or into an archived or
discarded fact (``FactTransitionRefused``).

``FactService.undo_merge(draft_id, *, expected_draft_version, expected_target_version)
-> FactView`` (the draft): versions and events are append-only, so undo appends a version to
the target that carries its wording from before the merge, records ``merge_undone`` on both
facts and brings the draft back with the link cleared.

``FactService.confirm_import(cv_import_id, *, expected_versions: Mapping[str, int])
-> tuple[FactView, ...]``: confirms the listed drafts of that import in one transaction, each
with its own ``confirmed`` event (and its draft role). A listed fact that changed since the
page was shown raises ``VersionConflict`` and nothing is confirmed; a fact that doesn't belong
to that import is refused the same way.

What was written is checked with SQL. Real migrated SQLite in a temp folder, ``FrozenClock``
and ``SequentialIdFactory``. Application modules are imported inside the tests so each fails
on its own while they are missing. All data is fictional.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import AppError, VersionConflict
from app.domain.facts import FactContent, FactStatus
from app.modules.storage import Database
from tests.fact_bank import (
    PROVIDER,
    START,
    Bank,
    add_import,
    assert_atomic,
    current_version_row,
    event_rows,
    event_types,
    fact_row,
    import_drafts,
    indexed_fact_ids,
    make_bank,
    open_migrated,
    role_row,
    skill_names_of,
    snapshot,
    typed,
    version_rows,
)
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

pytestmark = pytest.mark.integration

TARGET_TEXT = "Cut month-end close from nine days to four"
DRAFT_TEXT = "Reduced the month-end close to four days by automating reconciliations"
MERGED_TEXT = "Cut month-end close from nine days to four by automating reconciliations"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(START)


@pytest.fixture
def ids() -> SequentialIdFactory:
    return SequentialIdFactory("fb")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_migrated(tmp_path / "data" / "app.db", tmp_path / "backups")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def bank(db: Database, clock: FrozenClock, ids: SequentialIdFactory) -> Bank:
    return make_bank(db, clock, ids)


def _pair(bank: Bank, db: Database) -> tuple[Any, Any]:
    """A confirmed fact and an imported draft that says the same thing."""
    add_import(db, "imp-0001")
    target = typed(bank, TARGET_TEXT, skills=("Reconciliation", "Reporting"))
    (draft,) = import_drafts(
        bank, "imp-0001", [DRAFT_TEXT], skills=("reporting", "Process automation")
    )
    return target, draft


def _merge(bank: Bank, draft: Any, target: Any, text: str = MERGED_TEXT) -> Any:
    return bank.facts.merge(
        draft.id,
        target.id,
        FactContent(text=text),
        expected_draft_version=draft.version,
        expected_target_version=target.version,
    )


# --- merge -----------------------------------------------------------------------


def test_ac_m3_2_merge_adds_version_to_target_and_closes_draft(
    bank: Bank, db: Database, clock: FrozenClock
) -> None:
    target, draft = _pair(bank, db)
    target_first = version_rows(db, target.id)[0]
    draft_versions = version_rows(db, draft.id)
    clock.advance(timedelta(minutes=7))

    merged = _merge(bank, draft, target)

    # The target: a new version with the settled wording, still confirmed.
    assert merged.id == target.id
    assert merged.status == FactStatus.CONFIRMED
    assert merged.content_version == 2
    assert merged.content == FactContent(text=MERGED_TEXT)
    assert merged.version > target.version
    versions = version_rows(db, target.id)
    assert [(row["version"], row["text"], row["source"]) for row in versions] == [
        (1, TARGET_TEXT, "manual"),
        (2, MERGED_TEXT, "merge"),
    ]
    assert versions[0] == target_first
    assert fact_row(db, target.id)["current_version_id"] == versions[1]["id"]
    assert datetime.fromisoformat(versions[1]["created_at"]) == START + timedelta(minutes=7)
    # The user chose the wording, so it isn't attributed to the provider.
    assert versions[1]["provider"] is None
    target_event = event_rows(db, target.id)[-1]
    assert target_event["type"] == "merged"
    assert target_event["fact_version_id"] == versions[1]["id"]

    # Skills unioned: "reporting" on the draft is the target's "Reporting".
    assert merged.skills == ("Process automation", "Reconciliation", "Reporting")
    assert skill_names_of(db, target.id) == {"Process automation", "Reconciliation", "Reporting"}

    # The draft: closed as discarded, linked to the target, its wording kept as it was.
    closed = bank.facts.get(draft.id)
    assert closed.status == FactStatus.DISCARDED
    row = fact_row(db, draft.id)
    assert (row["status"], row["merged_into_fact_id"]) == ("discarded", target.id)
    assert row["version"] > draft.version
    assert version_rows(db, draft.id) == draft_versions
    assert current_version_row(db, draft.id)["provider"] == PROVIDER
    assert event_types(db, draft.id) == ["created", "merged_into"]
    assert draft.id not in indexed_fact_ids(db)
    assert target.id in indexed_fact_ids(db)


def test_merge_into_a_draft_keeps_the_target_a_draft(bank: Bank, db: Database) -> None:
    add_import(db, "imp-0001")
    target, draft = import_drafts(bank, "imp-0001", [TARGET_TEXT, DRAFT_TEXT])

    merged = _merge(bank, draft, target)

    # Merging is not a confirmation (AC-M3.1: nothing is confirmed without the user saying so).
    assert merged.status == FactStatus.DRAFT
    assert merged.content_version == 2
    assert fact_row(db, draft.id)["merged_into_fact_id"] == target.id


def _archived_target(bank: Bank) -> Any:
    fact = typed(bank, TARGET_TEXT)
    return bank.facts.archive(fact.id, expected_version=fact.version)


def _discarded_target(bank: Bank) -> Any:
    (fact,) = import_drafts(bank, "imp-0001", [TARGET_TEXT])
    return bank.facts.discard(fact.id, expected_version=fact.version)


@pytest.mark.parametrize(
    "make_target",
    [
        pytest.param(_archived_target, id="archived"),
        pytest.param(_discarded_target, id="discarded"),
    ],
)
def test_merge_into_archived_or_discarded_refused(
    bank: Bank, db: Database, make_target: Any
) -> None:
    from app.domain.errors import FactTransitionRefused

    add_import(db, "imp-0001")
    target = make_target(bank)
    (draft,) = import_drafts(bank, "imp-0001", [DRAFT_TEXT])
    before = snapshot(db)

    with pytest.raises(FactTransitionRefused) as refused:
        _merge(bank, draft, target)

    assert refused.value.kind == "FactTransitionRefused"
    assert snapshot(db) == before
    assert bank.facts.get(draft.id).status == FactStatus.DRAFT


def test_merge_of_a_fact_that_is_not_a_draft_or_into_itself_refused(
    bank: Bank, db: Database
) -> None:
    from app.domain.errors import FactTransitionRefused

    target, draft = _pair(bank, db)
    confirmed = typed(bank, "Edited the monthly staff newsletter")
    before = snapshot(db)

    with pytest.raises(FactTransitionRefused):
        _merge(bank, confirmed, target)
    with pytest.raises(FactTransitionRefused):
        _merge(bank, draft, draft)
    assert snapshot(db) == before


def test_merge_with_blank_wording_refused(bank: Bank, db: Database) -> None:
    from app.domain.errors import FactInputInvalid

    target, draft = _pair(bank, db)
    before = snapshot(db)
    with pytest.raises(FactInputInvalid):
        _merge(bank, draft, target, text="   ")
    assert snapshot(db) == before


@pytest.mark.parametrize("stale", ["draft", "target"])
def test_merge_with_a_stale_version_raises_conflict_and_writes_nothing(
    bank: Bank, db: Database, stale: str
) -> None:
    target, draft = _pair(bank, db)
    before = snapshot(db)

    with pytest.raises(VersionConflict):
        bank.facts.merge(
            draft.id,
            target.id,
            FactContent(text=MERGED_TEXT),
            expected_draft_version=draft.version + (1 if stale == "draft" else 0),
            expected_target_version=target.version + (1 if stale == "target" else 0),
        )

    assert snapshot(db) == before
    assert not db.connection.in_transaction


def test_merge_is_one_transaction(bank: Bank, db: Database, clock: FrozenClock) -> None:
    target, draft = _pair(bank, db)
    assert_atomic(db, clock, lambda fresh: _merge(fresh, draft, target))
    assert fact_row(db, draft.id)["merged_into_fact_id"] == target.id
    assert len(version_rows(db, target.id)) == 2


def test_merge_undo_appends_version_and_restores_draft(
    bank: Bank, db: Database, clock: FrozenClock
) -> None:
    target, draft = _pair(bank, db)
    merged = _merge(bank, draft, target)
    closed = bank.facts.get(draft.id)
    versions_after_merge = version_rows(db, target.id)
    clock.advance(timedelta(minutes=1))

    restored = bank.facts.undo_merge(
        draft.id, expected_draft_version=closed.version, expected_target_version=merged.version
    )

    # The draft is back, as it was.
    assert restored.id == draft.id
    assert restored.status == FactStatus.DRAFT
    assert restored.content == FactContent(text=DRAFT_TEXT)
    row = fact_row(db, draft.id)
    assert (row["status"], row["merged_into_fact_id"]) == ("draft", None)
    assert event_types(db, draft.id) == ["created", "merged_into", "merge_undone"]
    assert draft.id in indexed_fact_ids(db)
    assert skill_names_of(db, draft.id) == {"Process automation", "Reporting"}

    # The target reads as before the merge, through a third version: nothing is deleted.
    after = bank.facts.get(target.id)
    assert after.content == FactContent(text=TARGET_TEXT)
    assert after.content_version == 3
    assert after.status == FactStatus.CONFIRMED
    versions = version_rows(db, target.id)
    assert versions[:2] == versions_after_merge
    assert [(row["version"], row["text"]) for row in versions] == [
        (1, TARGET_TEXT),
        (2, MERGED_TEXT),
        (3, TARGET_TEXT),
    ]
    assert fact_row(db, target.id)["current_version_id"] == versions[2]["id"]
    assert event_types(db, target.id)[-2:] == ["merged", "merge_undone"]
    assert event_rows(db, target.id)[-1]["fact_version_id"] == versions[2]["id"]
    # Its skills are the ones it had before the merge.
    assert skill_names_of(db, target.id) == {"Reconciliation", "Reporting"}


def test_merge_undo_refused_for_a_draft_that_was_only_discarded(bank: Bank, db: Database) -> None:
    from app.domain.errors import FactTransitionRefused

    target, draft = _pair(bank, db)
    gone = bank.facts.discard(draft.id, expected_version=draft.version)
    before = snapshot(db)

    with pytest.raises(FactTransitionRefused):
        bank.facts.undo_merge(
            draft.id, expected_draft_version=gone.version, expected_target_version=target.version
        )
    assert snapshot(db) == before


def test_merge_undo_after_the_target_changed_raises_conflict(bank: Bank, db: Database) -> None:
    from app.services.facts import FactInput

    target, draft = _pair(bank, db)
    merged = _merge(bank, draft, target)
    closed = bank.facts.get(draft.id)
    bank.facts.edit(
        target.id,
        FactInput(text="Cut month-end close to four days", skills=merged.skills),
        expected_version=merged.version,
    )
    before = snapshot(db)

    with pytest.raises(VersionConflict):
        bank.facts.undo_merge(
            draft.id, expected_draft_version=closed.version, expected_target_version=merged.version
        )
    assert snapshot(db) == before


def test_plain_undo_discard_does_not_reopen_a_merged_draft(bank: Bank, db: Database) -> None:
    # Undoing a merge must also take the merge back out of the target, so the plain undo of
    # a discard is refused for a draft that was closed by a merge.
    from app.domain.errors import FactTransitionRefused

    target, draft = _pair(bank, db)
    _merge(bank, draft, target)
    closed = bank.facts.get(draft.id)
    before = snapshot(db)

    with pytest.raises(FactTransitionRefused):
        bank.facts.undo_discard(draft.id, expected_version=closed.version)
    assert snapshot(db) == before


# --- batch confirm ---------------------------------------------------------------


def _versions(drafts: list[Any]) -> dict[str, int]:
    return {draft.id: draft.version for draft in drafts}


def test_ac_m3_2_batch_confirm_by_source_confirms_only_that_source(
    bank: Bank, db: Database, clock: FrozenClock
) -> None:
    add_import(db, "imp-0001")
    add_import(db, "imp-0002")
    mine = import_drafts(
        bank,
        "imp-0001",
        ["Ran the quarterly stocktake", "Negotiated a new courier contract", "Kept the cash book"],
        role=("Lanternfish Logistics", "Operations Analyst"),
    )
    others = import_drafts(
        bank,
        "imp-0002",
        ["Edited the monthly staff newsletter"],
        role=("Brightwater Cooperative", "Volunteer treasurer"),
    )
    no_import = import_drafts(bank, None, ["Chaired the allotment committee"], source="session")
    already_gone = bank.facts.discard(mine[2].id, expected_version=mine[2].version)
    clock.advance(timedelta(minutes=4))

    confirmed = bank.facts.confirm_import("imp-0001", expected_versions=_versions(mine[:2]))

    assert {view.id for view in confirmed} == {mine[0].id, mine[1].id}
    assert all(view.status == FactStatus.CONFIRMED for view in confirmed)
    for draft in mine[:2]:
        assert fact_row(db, draft.id)["status"] == "confirmed"
        assert fact_row(db, draft.id)["version"] > draft.version
        # Versioned and recorded like a single confirm: the wording is untouched.
        assert len(version_rows(db, draft.id)) == 1
        assert event_types(db, draft.id) == ["created", "confirmed"]
        event = event_rows(db, draft.id)[-1]
        assert event["fact_version_id"] == current_version_row(db, draft.id)["id"]
        assert datetime.fromisoformat(event["created_at"]) == START + timedelta(minutes=4)
    assert role_row(db, mine[0].role_id)["status"] == "confirmed"

    # Nothing else moved: the other import, the session draft, the discarded draft.
    assert fact_row(db, others[0].id)["status"] == "draft"
    assert event_types(db, others[0].id) == ["created"]
    assert role_row(db, others[0].role_id)["status"] == "draft"
    assert fact_row(db, no_import[0].id)["status"] == "draft"
    assert fact_row(db, mine[2].id)["status"] == "discarded"
    assert fact_row(db, mine[2].id)["version"] == already_gone.version


def test_batch_confirm_is_all_or_nothing_on_conflict(
    bank: Bank, db: Database, clock: FrozenClock
) -> None:
    from app.services.facts import FactInput

    add_import(db, "imp-0001")
    drafts = import_drafts(
        bank,
        "imp-0001",
        ["Ran the quarterly stocktake", "Negotiated a new courier contract", "Kept the cash book"],
        role=("Lanternfish Logistics", "Operations Analyst"),
    )
    shown = _versions(drafts)
    # The last draft is edited in another tab after the page was shown.
    bank.facts.edit(
        drafts[2].id,
        FactInput(text="Kept the petty cash book", role_id=drafts[2].role_id),
        expected_version=drafts[2].version,
    )
    before = snapshot(db)

    with pytest.raises(VersionConflict):
        bank.facts.confirm_import("imp-0001", expected_versions=shown)

    assert snapshot(db) == before
    assert not db.connection.in_transaction
    assert [fact_row(db, draft.id)["status"] for draft in drafts] == ["draft", "draft", "draft"]
    assert role_row(db, drafts[0].role_id)["status"] == "draft"

    # With the versions as they are now, all three are confirmed together.
    current = {draft.id: bank.facts.get(draft.id).version for draft in drafts}
    done = bank.facts.confirm_import("imp-0001", expected_versions=current)
    assert len(done) == 3
    assert [fact_row(db, draft.id)["status"] for draft in drafts] == ["confirmed"] * 3


def test_batch_confirm_refuses_a_draft_discarded_meanwhile(bank: Bank, db: Database) -> None:
    add_import(db, "imp-0001")
    drafts = import_drafts(
        bank, "imp-0001", ["Ran the quarterly stocktake", "Negotiated a new courier contract"]
    )
    shown = _versions(drafts)
    bank.facts.discard(drafts[1].id, expected_version=drafts[1].version)
    before = snapshot(db)

    with pytest.raises(VersionConflict):
        bank.facts.confirm_import("imp-0001", expected_versions=shown)
    assert snapshot(db) == before


def test_batch_confirm_refuses_a_fact_from_another_source(bank: Bank, db: Database) -> None:
    add_import(db, "imp-0001")
    add_import(db, "imp-0002")
    mine = import_drafts(bank, "imp-0001", ["Ran the quarterly stocktake"])
    theirs = import_drafts(bank, "imp-0002", ["Edited the monthly staff newsletter"])
    before = snapshot(db)

    with pytest.raises(AppError):
        bank.facts.confirm_import("imp-0001", expected_versions=_versions(mine + theirs))

    assert snapshot(db) == before
    assert fact_row(db, mine[0].id)["status"] == "draft"
    assert fact_row(db, theirs[0].id)["status"] == "draft"


def test_batch_confirm_is_one_transaction(bank: Bank, db: Database, clock: FrozenClock) -> None:
    add_import(db, "imp-0001")
    drafts = import_drafts(
        bank,
        "imp-0001",
        ["Ran the quarterly stocktake", "Negotiated a new courier contract", "Kept the cash book"],
    )
    assert_atomic(
        db,
        clock,
        lambda fresh: fresh.facts.confirm_import("imp-0001", expected_versions=_versions(drafts)),
    )
    assert [fact_row(db, draft.id)["status"] for draft in drafts] == ["confirmed"] * 3
