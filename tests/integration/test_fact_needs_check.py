"""A draft's ``needs_check`` flag is kept until the user confirms that draft themselves (M3.3).

- A plain edit of a draft carries the flag from the draft's previous current version.
- Confirming one draft is the user's own check: ``confirm`` writes no version and
  ``edit_and_confirm`` writes its new version unflagged.
- ``FactView.needs_check`` is true only for a draft whose current version is flagged. Version
  rows are append-only, so a confirmed fact's row may keep the flag; its view never reports it.
- ``confirm_import`` leaves flagged drafts as drafts and returns only the facts it confirmed.

What was written is checked with SQL. Real migrated SQLite in a temp folder, ``FrozenClock``
and ``SequentialIdFactory``. Application modules are imported inside the tests so each fails
on its own while they are missing. All data is fictional.
"""

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import VersionConflict
from app.domain.facts import FactContent, FactStatus
from app.modules.storage import Database
from tests.fact_bank import (
    MODEL,
    PROMPT_ID,
    PROMPT_VERSION,
    PROVIDER,
    START,
    Bank,
    add_import,
    assert_atomic,
    current_version_row,
    event_types,
    fact_row,
    make_bank,
    open_migrated,
    role_row,
    snapshot,
    typed,
    version_rows,
)
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

pytestmark = pytest.mark.integration

IMPORT = "imp-0001"
ROLE = ("Lanternfish Logistics", "Operations Analyst")

STOCKTAKE = "Ran the quarterly stocktake across 3 depots"
STOCKTAKE_V2 = "Ran the quarterly stocktake across three depots"
STOCKTAKE_V3 = "Led the quarterly stocktake across three depots"
COURIER = "Negotiated a new courier contract"
CASH_BOOK = "Kept the cash book"
TARGET_TEXT = "Cut month-end close from nine days to four"


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


def _text(text: str, **fields: Any) -> Any:
    from app.services.facts import FactInput

    return FactInput(text=text, **fields)


def _imported(
    bank: Bank,
    db: Database,
    drafts: Sequence[tuple[str, bool]],
    *,
    role: tuple[str, str] | None = None,
) -> list[Any]:
    """Drafts of one import, each ``(text, needs_check)``, optionally under one draft role."""
    from app.services.facts import DraftInput, DraftOrigin, DraftRole

    add_import(db, IMPORT)
    origin = DraftOrigin(
        source="cv_import",
        provider=PROVIDER,
        model=MODEL,
        prompt_id=PROMPT_ID,
        prompt_version=PROMPT_VERSION,
        cv_import_id=IMPORT,
    )
    roles = [] if role is None else [DraftRole(key="r1", employer=role[0], title=role[1])]
    inputs = [
        DraftInput(
            text=text,
            role_key=None if role is None else "r1",
            original_answer=f"CV line: {text}",
            needs_check=flagged,
        )
        for text, flagged in drafts
    ]
    return list(bank.facts.create_drafts(origin, inputs, roles=roles))


def _versions(drafts: Sequence[Any]) -> dict[str, int]:
    return {draft.id: draft.version for draft in drafts}


def _merge(bank: Bank, draft: Any, target: Any, text: str) -> Any:
    return bank.facts.merge(
        draft.id,
        target.id,
        FactContent(text=text),
        expected_draft_version=draft.version,
        expected_target_version=target.version,
    )


# --- editing a draft -------------------------------------------------------------


def test_needs_check_survives_two_edits_of_a_draft(bank: Bank, db: Database) -> None:
    (draft,) = _imported(bank, db, [(STOCKTAKE, True)])
    assert draft.needs_check is True

    second = bank.facts.edit(draft.id, _text(STOCKTAKE_V2), expected_version=draft.version)
    third = bank.facts.edit(draft.id, _text(STOCKTAKE_V3), expected_version=second.version)

    for view in (second, third, bank.facts.get(draft.id)):
        assert view.status == FactStatus.DRAFT
        assert view.needs_check is True
        # The wording is the user's own; the fact still began with the provider.
        assert (view.provider, view.model) == (None, None)
        assert (view.origin_provider, view.origin_model) == (PROVIDER, MODEL)
    assert [
        (row["version"], row["source"], row["provider"], row["needs_check"])
        for row in version_rows(db, draft.id)
    ] == [
        (1, "cv_import", PROVIDER, 1),
        (2, "manual", None, 1),
        (3, "manual", None, 1),
    ]


def test_editing_an_unflagged_draft_does_not_flag_it(bank: Bank, db: Database) -> None:
    (draft,) = _imported(bank, db, [(STOCKTAKE, False)])

    edited = bank.facts.edit(draft.id, _text(STOCKTAKE_V2), expected_version=draft.version)

    assert edited.needs_check is False
    assert [row["needs_check"] for row in version_rows(db, draft.id)] == [0, 0]


def test_details_only_edit_of_a_flagged_draft_keeps_the_flag(bank: Bank, db: Database) -> None:
    (draft,) = _imported(bank, db, [(STOCKTAKE, True)])

    edited = bank.facts.edit(
        draft.id, _text(STOCKTAKE, tags=("stock",)), expected_version=draft.version
    )

    assert edited.needs_check is True
    assert len(version_rows(db, draft.id)) == 1


# --- confirming one draft --------------------------------------------------------


def test_confirming_a_flagged_draft_is_the_users_check(bank: Bank, db: Database) -> None:
    (draft,) = _imported(bank, db, [(STOCKTAKE, True)], role=ROLE)

    confirmed = bank.facts.confirm(draft.id, expected_version=draft.version)

    assert confirmed.status == FactStatus.CONFIRMED
    for view in (confirmed, bank.facts.get(draft.id)):
        assert view.needs_check is False
    # No new version: the append-only row keeps what the import found.
    assert [(row["version"], row["needs_check"]) for row in version_rows(db, draft.id)] == [(1, 1)]
    assert event_types(db, draft.id) == ["created", "confirmed"]
    assert role_row(db, draft.role_id)["status"] == "confirmed"
    # The version history still shows the flag as stored.
    assert [version.needs_check for version in bank.facts.versions(draft.id)] == [True]


def test_confirmed_fact_reworded_later_never_reports_the_flag(bank: Bank, db: Database) -> None:
    (draft,) = _imported(bank, db, [(STOCKTAKE, True)])
    confirmed = bank.facts.confirm(draft.id, expected_version=draft.version)

    edited = bank.facts.edit(draft.id, _text(STOCKTAKE_V2), expected_version=confirmed.version)

    assert edited.status == FactStatus.CONFIRMED
    assert edited.needs_check is False
    assert [row["needs_check"] for row in version_rows(db, draft.id)] == [1, 0]


def test_edit_and_confirm_clears_the_flag_on_the_new_version(bank: Bank, db: Database) -> None:
    (draft,) = _imported(bank, db, [(STOCKTAKE, True)])
    edited = bank.facts.edit(draft.id, _text(STOCKTAKE_V2), expected_version=draft.version)
    assert edited.needs_check is True

    confirmed = bank.facts.edit_and_confirm(
        draft.id, _text(STOCKTAKE_V3), expected_version=edited.version
    )

    assert confirmed.status == FactStatus.CONFIRMED
    assert confirmed.needs_check is False
    assert bank.facts.get(draft.id).needs_check is False
    assert [(row["version"], row["needs_check"]) for row in version_rows(db, draft.id)] == [
        (1, 1),
        (2, 1),
        (3, 0),
    ]
    assert current_version_row(db, draft.id)["text"] == STOCKTAKE_V3


def test_edit_and_confirm_with_the_same_wording_no_longer_reports_the_flag(
    bank: Bank, db: Database
) -> None:
    (draft,) = _imported(bank, db, [(STOCKTAKE, True)])

    confirmed = bank.facts.edit_and_confirm(
        draft.id, _text(STOCKTAKE), expected_version=draft.version
    )

    assert confirmed.status == FactStatus.CONFIRMED
    assert confirmed.needs_check is False
    assert [(row["version"], row["needs_check"]) for row in version_rows(db, draft.id)] == [(1, 1)]


def test_discarded_flagged_draft_reports_the_flag_again_once_brought_back(
    bank: Bank, db: Database
) -> None:
    (draft,) = _imported(bank, db, [(STOCKTAKE, True)])

    gone = bank.facts.discard(draft.id, expected_version=draft.version)
    back = bank.facts.undo_discard(draft.id, expected_version=gone.version)

    assert gone.needs_check is False
    assert (back.status, back.needs_check) == (FactStatus.DRAFT, True)


# --- batch confirm ---------------------------------------------------------------


def test_batch_confirm_skips_flagged_drafts_and_confirms_the_others(
    bank: Bank, db: Database
) -> None:
    plain_a, flagged, plain_b = _imported(
        bank, db, [(STOCKTAKE, False), (COURIER, True), (CASH_BOOK, False)], role=ROLE
    )
    assert role_row(db, plain_a.role_id)["status"] == "draft"
    flagged_before = fact_row(db, flagged.id)

    confirmed = bank.facts.confirm_import(
        IMPORT, expected_versions=_versions([plain_a, flagged, plain_b])
    )

    # Only the facts confirmed come back, in the order they were listed.
    assert [view.id for view in confirmed] == [plain_a.id, plain_b.id]
    assert all(view.status == FactStatus.CONFIRMED for view in confirmed)
    for draft in (plain_a, plain_b):
        assert fact_row(db, draft.id)["status"] == "confirmed"
        assert event_types(db, draft.id) == ["created", "confirmed"]
    assert role_row(db, plain_a.role_id)["status"] == "confirmed"

    # The flagged draft is left exactly as it was: not confirmed, and not an error.
    assert fact_row(db, flagged.id) == flagged_before
    assert event_types(db, flagged.id) == ["created"]
    still = bank.facts.get(flagged.id)
    assert (still.status, still.needs_check, still.version) == (
        FactStatus.DRAFT,
        True,
        flagged.version,
    )

    # The user's own confirm of it still works afterwards.
    checked = bank.facts.confirm(flagged.id, expected_version=flagged.version)
    assert (checked.status, checked.needs_check) == (FactStatus.CONFIRMED, False)


def test_batch_confirm_with_flagged_drafts_is_one_transaction(
    bank: Bank, db: Database, clock: FrozenClock
) -> None:
    drafts = _imported(
        bank, db, [(STOCKTAKE, False), (COURIER, True), (CASH_BOOK, False)], role=ROLE
    )

    assert_atomic(
        db,
        clock,
        lambda fresh: fresh.facts.confirm_import(IMPORT, expected_versions=_versions(drafts)),
    )

    assert [fact_row(db, draft.id)["status"] for draft in drafts] == [
        "confirmed",
        "draft",
        "confirmed",
    ]
    assert role_row(db, drafts[0].role_id)["status"] == "confirmed"


def test_batch_confirm_of_only_flagged_drafts_changes_nothing(bank: Bank, db: Database) -> None:
    drafts = _imported(bank, db, [(STOCKTAKE, True), (COURIER, True)], role=ROLE)
    before = snapshot(db)

    confirmed = bank.facts.confirm_import(IMPORT, expected_versions=_versions(drafts))

    assert confirmed == ()
    assert snapshot(db) == before
    assert not db.connection.in_transaction
    assert role_row(db, drafts[0].role_id)["status"] == "draft"


def test_batch_confirm_skips_a_draft_whose_flag_survived_an_edit(bank: Bank, db: Database) -> None:
    plain, flagged = _imported(bank, db, [(STOCKTAKE, False), (COURIER, True)])
    edited = bank.facts.edit(
        flagged.id, _text("Negotiated the courier contract"), expected_version=flagged.version
    )

    confirmed = bank.facts.confirm_import(IMPORT, expected_versions=_versions([plain, edited]))

    assert [view.id for view in confirmed] == [plain.id]
    assert fact_row(db, flagged.id)["status"] == "draft"


def test_batch_confirm_with_flagged_drafts_is_still_all_or_nothing_on_conflict(
    bank: Bank, db: Database
) -> None:
    plain_a, flagged, plain_b = _imported(
        bank, db, [(STOCKTAKE, False), (COURIER, True), (CASH_BOOK, False)], role=ROLE
    )
    shown = _versions([plain_a, flagged, plain_b])
    # An unflagged draft is edited in another tab after the page was shown.
    bank.facts.edit(
        plain_b.id,
        _text("Kept the petty cash book", role_id=plain_b.role_id),
        expected_version=plain_b.version,
    )
    before = snapshot(db)

    with pytest.raises(VersionConflict):
        bank.facts.confirm_import(IMPORT, expected_versions=shown)

    assert snapshot(db) == before
    assert not db.connection.in_transaction
    assert role_row(db, plain_a.role_id)["status"] == "draft"


# --- merging ---------------------------------------------------------------------


def test_confirmed_target_that_took_a_flagged_drafts_wording_does_not_report_the_flag(
    bank: Bank, db: Database
) -> None:
    from app.services.fact_library import LibraryQuery

    target = typed(bank, TARGET_TEXT)
    (draft,) = _imported(bank, db, [(STOCKTAKE, True)])

    merged = _merge(bank, draft, target, STOCKTAKE)

    assert merged.status == FactStatus.CONFIRMED
    # The version row keeps what made the wording; the confirmed fact doesn't ask for a check.
    version = current_version_row(db, target.id)
    assert (version["source"], version["provider"], version["needs_check"]) == (
        "merge",
        PROVIDER,
        1,
    )
    assert merged.needs_check is False
    assert bank.facts.get(target.id).needs_check is False
    found = bank.library.search(LibraryQuery(status=FactStatus.CONFIRMED))
    assert [(item.id, item.needs_check) for item in found.items] == [(target.id, False)]
