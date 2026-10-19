"""The fact service: create, edit, confirm, discard, archive and privacy (M3.3; AC-M3.2 and
AC-M3.3 at service level; spec 3.2, 3.4, 3.5; ADR-089 item 11, ADR-090).

``app.services.facts.FactService(db, clock, ids)``. Every write takes the ``expected_version``
the caller last saw (``FactView.version``, the row's optimistic counter) and runs in one
transaction together with its ``fact_events`` row; a stale version raises ``VersionConflict``
and writes nothing.

- ``create(data: FactInput, *, source="manual") -> FactView``: what the user types is confirmed
  at once; event ``created``.
- ``create_drafts(origin: DraftOrigin, drafts, *, roles=()) -> tuple[FactView, ...]``: drafts
  from an AI task, with draft roles named by ``DraftRole.key``; event ``created``.
- ``edit(fact_id, data, *, expected_version) -> FactView``: changed wording adds a version
  (source ``manual``) and the event ``edited``; changed role, skills, tags or dates alone add
  the event ``details_changed`` and no version; an identical edit adds no version.
- ``confirm``, ``edit_and_confirm``, ``discard``, ``undo_discard``, ``archive``, ``restore``:
  events ``confirmed``, ``edited`` then ``confirmed``, ``discarded``, ``discard_undone``,
  ``archived``, ``restored``. A move the lifecycle refuses raises ``FactTransitionRefused``.
- ``set_privacy(fact_id, privacy, *, expected_version)``: event ``privacy_changed`` with the
  detail ``{"from": ..., "to": ...}``.
- ``get(fact_id) -> FactView`` and ``versions(fact_id)``, oldest first.

``FactView`` fields used here: ``id``, ``number``, ``type``, ``status``, ``privacy``,
``role_id``, ``tags``, ``start_date``, ``end_date``, ``skills`` (names), ``version`` (the row),
``content_version`` (the 3 in ``F12@3``), ``content`` (``FactContent``), ``cv_import_id``.

What was written is checked with SQL, so the implementation is free inside. Real migrated
SQLite in a temp folder, ``FrozenClock`` and ``SequentialIdFactory``. Application modules are
imported inside the tests so each fails on its own while they are missing. All data is
fictional.
"""

import json
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import AppError, VersionConflict
from app.domain.facts import FactContent, FactStatus, FactType, Privacy
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
    count,
    current_version_row,
    event_rows,
    event_types,
    fact_row,
    import_drafts,
    indexed_fact_ids,
    make_bank,
    open_migrated,
    role_row,
    second_connection,
    snapshot,
    typed,
    version_rows,
)
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

pytestmark = pytest.mark.integration

CLOSE = "Cut month-end close from nine days to four"
CLOSE_V2 = "Cut month-end close from nine working days to four"
CLOSE_V3 = "Cut month-end close from nine working days to four by automating reconciliations"
STOCKTAKE = "Ran the quarterly stocktake across three depots"
NEWSLETTER = "Edited the monthly staff newsletter"


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


def _draft(bank: Bank, db: Database, text: str = STOCKTAKE) -> Any:
    add_import(db, "imp-0001")
    return import_drafts(bank, "imp-0001", [text])[0]


def _at(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


# --- create ----------------------------------------------------------------------


def test_typed_fact_is_confirmed_at_once_with_first_version_and_event(
    bank: Bank, db: Database, clock: FrozenClock
) -> None:
    view = typed(bank, f"  {CLOSE}\r\n")

    assert view.status == FactStatus.CONFIRMED
    assert view.type == FactType.FACT
    assert view.privacy == Privacy.NORMAL
    assert view.number == 1
    assert view.content_version == 1
    assert view.content == FactContent(text=CLOSE)
    assert bank.facts.get(view.id) == view

    row = fact_row(db, view.id)
    assert row["status"] == "confirmed"
    assert row["version"] == view.version
    assert _at(row["created_at"]) == clock.now()
    assert _at(row["updated_at"]) == clock.now()

    versions = version_rows(db, view.id)
    assert len(versions) == 1
    first = versions[0]
    assert first["id"] == row["current_version_id"]
    assert (first["version"], first["text"], first["source"]) == (1, CLOSE, "manual")
    # A fact the user typed has nothing that generated it (ADR-089 item 8).
    assert [first[name] for name in ("provider", "model", "prompt_id", "prompt_version")] == [
        None,
        None,
        None,
        None,
    ]
    assert first["needs_check"] == 0
    assert _at(first["created_at"]) == clock.now()

    events = event_rows(db, view.id)
    assert events[0]["type"] == "created"
    assert events[0]["fact_version_id"] == first["id"]
    assert all(_at(event["created_at"]) == clock.now() for event in events)
    assert view.id in indexed_fact_ids(db)


def test_typed_story_keeps_its_parts(bank: Bank, db: Database) -> None:
    from app.services.facts import FactInput

    view = bank.facts.create(
        FactInput(
            type=FactType.STORY,
            situation="The depot's picking backlog had grown to a fortnight",
            action=" Rebuilt the picking rota around delivery windows ",
            result="Backlog cleared within a month",
            tags=("operations", "rota"),
            start_date="2023-04",
            end_date="2023-06-30",
        )
    )

    assert view.type == FactType.STORY
    assert view.content == FactContent(
        situation="The depot's picking backlog had grown to a fortnight",
        action="Rebuilt the picking rota around delivery windows",
        result="Backlog cleared within a month",
    )
    assert view.tags == ("operations", "rota")
    assert (view.start_date, view.end_date) == ("2023-04", "2023-06-30")
    row = fact_row(db, view.id)
    assert row["type"] == "story"
    assert json.loads(row["tags"]) == ["operations", "rota"]
    stored = current_version_row(db, view.id)
    assert stored["text"] is None
    assert stored["action"] == "Rebuilt the picking rota around delivery windows"


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"text": "   \r\n"}, id="blank-text"),
        pytest.param({"text": None}, id="nothing"),
        pytest.param(
            {"type": FactType.STORY, "situation": " ", "action": "", "result": None},
            id="blank-story",
        ),
    ],
)
def test_blank_content_refused_and_writes_nothing(
    bank: Bank, db: Database, fields: dict[str, Any]
) -> None:
    from app.domain.errors import FactInputInvalid
    from app.services.facts import FactInput

    before = snapshot(db)
    with pytest.raises(FactInputInvalid) as refused:
        bank.facts.create(FactInput(**fields))
    assert refused.value.field == "text"
    assert refused.value.user_message
    assert snapshot(db) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start_date", "2023-02-29"),
        ("start_date", "2024-13"),
        ("end_date", "2024-04-31"),
        ("end_date", "March 2024"),
    ],
)
def test_impossible_dates_refused_on_create_and_edit(
    bank: Bank, db: Database, field: str, value: str
) -> None:
    from app.domain.errors import FactInputInvalid

    with pytest.raises(FactInputInvalid) as refused:
        typed(bank, CLOSE, **{field: value})
    assert refused.value.field == field
    assert count(db, "facts") == 0

    kept = typed(bank, CLOSE)
    before = snapshot(db)
    with pytest.raises(FactInputInvalid) as refused:
        bank.facts.edit(kept.id, _text(CLOSE_V2, **{field: value}), expected_version=kept.version)
    assert refused.value.field == field
    assert snapshot(db) == before


def test_drafts_carry_their_origin_and_stay_drafts(
    bank: Bank, db: Database, clock: FrozenClock
) -> None:
    from app.services.facts import DraftInput, DraftOrigin, DraftRole

    add_import(db, "imp-0001")
    origin = DraftOrigin(
        source="cv_import",
        provider=PROVIDER,
        model=MODEL,
        prompt_id=PROMPT_ID,
        prompt_version=PROMPT_VERSION,
        cv_import_id="imp-0001",
    )
    made = bank.facts.create_drafts(
        origin,
        [
            DraftInput(
                text=STOCKTAKE,
                role_key="ops",
                skills=("Inventory control",),
                original_answer="Quarterly stocktake, 3 depots",
                needs_check=True,
                start_date="2022",
            ),
            DraftInput(
                type=FactType.STORY,
                situation="Volunteers kept double-booking the hall",
                action="Set up a shared booking ledger",
                result="No clashes for a year",
                role_key=None,
                original_answer="Booking ledger for the hall",
            ),
        ],
        roles=[
            DraftRole(
                key="ops",
                employer="Lanternfish Logistics",
                title="Operations Analyst",
                start_date="2021-09",
                end_date=None,
            )
        ],
    )

    assert [view.status for view in made] == [FactStatus.DRAFT, FactStatus.DRAFT]
    assert [view.number for view in made] == [1, 2]
    assert [view.cv_import_id for view in made] == ["imp-0001", "imp-0001"]
    assert made[0].content == FactContent(text=STOCKTAKE)
    assert made[0].skills == ("Inventory control",)
    assert made[0].start_date == "2022"
    assert made[1].type == FactType.STORY
    assert made[1].role_id is None

    first = current_version_row(db, made[0].id)
    assert first["version"] == 1
    assert first["source"] == "cv_import"
    assert (first["provider"], first["model"]) == (PROVIDER, MODEL)
    assert (first["prompt_id"], first["prompt_version"]) == (PROMPT_ID, PROMPT_VERSION)
    assert first["original_answer"] == "Quarterly stocktake, 3 depots"
    assert first["needs_check"] == 1
    assert current_version_row(db, made[1].id)["needs_check"] == 0

    assert made[0].role_id is not None
    role = role_row(db, made[0].role_id)
    assert (role["employer"], role["title"], role["status"]) == (
        "Lanternfish Logistics",
        "Operations Analyst",
        "draft",
    )
    assert (role["start_date"], role["end_date"]) == ("2021-09", None)
    assert _at(role["created_at"]) == clock.now()

    for view in made:
        assert fact_row(db, view.id)["status"] == "draft"
        assert event_types(db, view.id) == ["created"]
    # AC-M3.1's groundwork: nothing an import writes is confirmed.
    assert count(db, "facts") == 2
    assert fact_row(db, made[0].id)["cv_import_id"] == "imp-0001"


def test_draft_naming_an_unknown_role_key_writes_nothing(bank: Bank, db: Database) -> None:
    from app.services.facts import DraftInput, DraftOrigin

    before = snapshot(db)
    with pytest.raises((AppError, ValueError)):
        bank.facts.create_drafts(
            DraftOrigin(source="cv_import"),
            [DraftInput(text=STOCKTAKE), DraftInput(text=NEWSLETTER, role_key="missing")],
        )
    assert snapshot(db) == before


# --- edit: versions --------------------------------------------------------------


def test_ac_m3_3_every_edit_creates_a_version(bank: Bank, db: Database, clock: FrozenClock) -> None:
    created = typed(bank, CLOSE)
    first_row = version_rows(db, created.id)[0]

    clock.advance(timedelta(minutes=5))
    second = bank.facts.edit(created.id, _text(CLOSE_V2), expected_version=created.version)
    clock.advance(timedelta(minutes=5))
    third = bank.facts.edit(created.id, _text(CLOSE_V3), expected_version=second.version)

    assert (second.content_version, third.content_version) == (2, 3)
    assert third.content == FactContent(text=CLOSE_V3)
    assert created.version < second.version < third.version
    assert third.number == created.number
    # Editing a confirmed fact is not a status change.
    assert third.status == FactStatus.CONFIRMED
    assert bank.facts.get(created.id) == third

    versions = version_rows(db, created.id)
    assert [(row["version"], row["text"]) for row in versions] == [
        (1, CLOSE),
        (2, CLOSE_V2),
        (3, CLOSE_V3),
    ]
    # Earlier versions are kept exactly as they were written.
    assert versions[0] == first_row
    assert [row["source"] for row in versions] == ["manual", "manual", "manual"]
    assert len({row["id"] for row in versions}) == 3
    assert _at(versions[2]["created_at"]) == START + timedelta(minutes=10)

    row = fact_row(db, created.id)
    assert row["current_version_id"] == versions[2]["id"]
    assert row["version"] == third.version
    assert _at(row["updated_at"]) == START + timedelta(minutes=10)
    assert _at(row["created_at"]) == START

    edits = [event for event in event_rows(db, created.id) if event["type"] == "edited"]
    assert [event["fact_version_id"] for event in edits] == [versions[1]["id"], versions[2]["id"]]
    assert [_at(event["created_at"]) for event in edits] == [
        START + timedelta(minutes=5),
        START + timedelta(minutes=10),
    ]


def test_ac_m3_3_editing_a_draft_keeps_it_a_draft_and_keeps_the_ai_wording(
    bank: Bank, db: Database
) -> None:
    draft = _draft(bank, db)
    edited = bank.facts.edit(
        draft.id, _text("Ran the quarterly stocktake"), expected_version=draft.version
    )

    assert edited.status == FactStatus.DRAFT
    assert edited.content_version == 2
    versions = version_rows(db, draft.id)
    assert (versions[0]["source"], versions[0]["provider"], versions[0]["text"]) == (
        "cv_import",
        PROVIDER,
        STOCKTAKE,
    )
    # The user's own wording isn't attributed to a provider.
    assert (versions[1]["source"], versions[1]["provider"], versions[1]["model"]) == (
        "manual",
        None,
        None,
    )
    assert versions[1]["needs_check"] == 0


@pytest.mark.parametrize(
    "same",
    [
        pytest.param(CLOSE, id="same-text"),
        pytest.param(f"  {CLOSE}  ", id="surrounding-space"),
        pytest.param(f"{CLOSE}\r\n", id="browser-line-ending"),
    ],
)
def test_identical_edit_makes_no_version(bank: Bank, db: Database, same: str) -> None:
    created = typed(bank, CLOSE)
    version_before = version_rows(db, created.id)

    after = bank.facts.edit(created.id, _text(same), expected_version=created.version)

    assert after.content_version == 1
    assert after.content == FactContent(text=CLOSE)
    assert version_rows(db, created.id) == version_before
    assert "edited" not in event_types(db, created.id)


def test_edit_stores_parts_cleaned(bank: Bank, db: Database) -> None:
    from app.services.facts import FactInput

    created = typed(bank, CLOSE)
    bank.facts.edit(
        created.id,
        FactInput(text="Line one\r\nLine two  ", situation="   ", action="", result=None),
        expected_version=created.version,
    )

    stored = current_version_row(db, created.id)
    assert stored["text"] == "Line one\nLine two"
    assert (stored["situation"], stored["action"], stored["result"]) == (None, None, None)


def test_edit_to_blank_content_refused_and_writes_nothing(bank: Bank, db: Database) -> None:
    from app.domain.errors import FactInputInvalid

    created = typed(bank, CLOSE)
    before = snapshot(db)
    with pytest.raises(FactInputInvalid):
        bank.facts.edit(created.id, _text("  "), expected_version=created.version)
    assert snapshot(db) == before


def test_changing_details_only_is_an_event_not_a_version(bank: Bank, db: Database) -> None:
    role = bank.roles.create(employer="Lanternfish Logistics", title="Operations Analyst")
    created = typed(bank, CLOSE)

    after = bank.facts.edit(
        created.id,
        _text(
            CLOSE,
            role_id=role.id,
            tags=("finance",),
            start_date="2023",
            end_date="2023-11",
            skills=("Reconciliation",),
        ),
        expected_version=created.version,
    )

    # ADR-090 item 4: role, skills, tags and dates live on the fact, not on a version.
    assert after.content_version == 1
    assert len(version_rows(db, created.id)) == 1
    assert after.version > created.version
    assert (after.role_id, after.tags, after.start_date, after.end_date, after.skills) == (
        role.id,
        ("finance",),
        "2023",
        "2023-11",
        ("Reconciliation",),
    )
    row = fact_row(db, created.id)
    assert (row["role_id"], json.loads(row["tags"]), row["start_date"], row["end_date"]) == (
        role.id,
        ["finance"],
        "2023",
        "2023-11",
    )
    assert event_types(db, created.id)[-1] == "details_changed"
    assert "edited" not in event_types(db, created.id)


def test_fact_with_unknown_role_refused_and_writes_nothing(bank: Bank, db: Database) -> None:
    from app.domain.errors import FactInputInvalid

    before = snapshot(db)
    with pytest.raises(FactInputInvalid) as refused:
        typed(bank, CLOSE, role_id="role-that-does-not-exist")
    assert refused.value.field == "role_id"
    assert snapshot(db) == before


def test_versions_listed_oldest_first_even_when_their_times_tie(bank: Bank, db: Database) -> None:
    # The clock never moves here, so every created_at is equal and only the insertion order
    # (rowid) can put the history in order (ADR-090 item 3).
    created = typed(bank, CLOSE)
    second = bank.facts.edit(created.id, _text(CLOSE_V2), expected_version=created.version)
    bank.facts.edit(created.id, _text(CLOSE_V3), expected_version=second.version)

    listed = bank.facts.versions(created.id)

    assert [entry.version for entry in listed] == [1, 2, 3]
    assert [entry.content for entry in listed] == [
        FactContent(text=CLOSE),
        FactContent(text=CLOSE_V2),
        FactContent(text=CLOSE_V3),
    ]
    assert [entry.source for entry in listed] == ["manual", "manual", "manual"]
    assert [entry.created_at for entry in listed] == [START, START, START]


# --- confirm ---------------------------------------------------------------------


def test_ac_m3_2_confirm_and_edit_then_confirm_recorded(
    bank: Bank, db: Database, clock: FrozenClock
) -> None:
    add_import(db, "imp-0001")
    plain, reworded, unchanged = import_drafts(
        bank, "imp-0001", [STOCKTAKE, NEWSLETTER, "Trained six volunteers on the booking ledger"]
    )
    clock.advance(timedelta(hours=1))

    # Confirm: the status changes, the wording doesn't.
    confirmed = bank.facts.confirm(plain.id, expected_version=plain.version)
    assert confirmed.status == FactStatus.CONFIRMED
    assert confirmed.content_version == 1
    assert confirmed.version > plain.version
    assert fact_row(db, plain.id)["status"] == "confirmed"
    assert len(version_rows(db, plain.id)) == 1
    assert event_types(db, plain.id) == ["created", "confirmed"]
    confirm_event = event_rows(db, plain.id)[-1]
    assert confirm_event["fact_version_id"] == current_version_row(db, plain.id)["id"]
    assert _at(confirm_event["created_at"]) == START + timedelta(hours=1)

    # Edit then confirm: one step that adds the user's wording as version 2 and confirms it.
    new_text = "Edited and laid out the monthly staff newsletter"
    done = bank.facts.edit_and_confirm(
        reworded.id, _text(new_text), expected_version=reworded.version
    )
    assert done.status == FactStatus.CONFIRMED
    assert done.content_version == 2
    assert done.content == FactContent(text=new_text)
    versions = version_rows(db, reworded.id)
    assert [(row["version"], row["text"], row["source"]) for row in versions] == [
        (1, NEWSLETTER, "cv_import"),
        (2, new_text, "manual"),
    ]
    assert versions[0]["provider"] == PROVIDER
    assert versions[1]["provider"] is None
    assert fact_row(db, reworded.id)["current_version_id"] == versions[1]["id"]
    assert event_types(db, reworded.id) == ["created", "edited", "confirmed"]
    recorded = event_rows(db, reworded.id)
    assert [event["fact_version_id"] for event in recorded[1:]] == [
        versions[1]["id"],
        versions[1]["id"],
    ]

    # With nothing changed it is a plain confirm: no version for an identical edit.
    same = bank.facts.edit_and_confirm(
        unchanged.id,
        _text("Trained six volunteers on the booking ledger"),
        expected_version=unchanged.version,
    )
    assert same.status == FactStatus.CONFIRMED
    assert same.content_version == 1
    assert len(version_rows(db, unchanged.id)) == 1
    assert event_types(db, unchanged.id) == ["created", "confirmed"]


def test_edit_and_confirm_is_one_transaction(bank: Bank, db: Database, clock: FrozenClock) -> None:
    draft = _draft(bank, db)
    assert_atomic(
        db,
        clock,
        lambda fresh: fresh.facts.edit_and_confirm(
            draft.id, _text("Ran the quarterly stocktake"), expected_version=draft.version
        ),
    )
    assert fact_row(db, draft.id)["status"] == "confirmed"
    assert len(version_rows(db, draft.id)) == 2


# --- discard, archive ------------------------------------------------------------


def test_discard_then_undo_restores_draft(bank: Bank, db: Database) -> None:
    draft = _draft(bank, db)
    versions_before = version_rows(db, draft.id)

    discarded = bank.facts.discard(draft.id, expected_version=draft.version)

    assert discarded.status == FactStatus.DISCARDED
    assert fact_row(db, draft.id)["status"] == "discarded"
    assert event_types(db, draft.id) == ["created", "discarded"]
    assert draft.id not in indexed_fact_ids(db)

    restored = bank.facts.undo_discard(draft.id, expected_version=discarded.version)

    assert restored.status == FactStatus.DRAFT
    assert restored.content == draft.content
    assert restored.content_version == 1
    assert restored.cv_import_id == "imp-0001"
    row = fact_row(db, draft.id)
    assert (row["status"], row["merged_into_fact_id"]) == ("draft", None)
    # Undo is a compensating event: the history keeps both steps (ADR-090 item 3).
    assert event_types(db, draft.id) == ["created", "discarded", "discard_undone"]
    assert version_rows(db, draft.id) == versions_before
    assert draft.id in indexed_fact_ids(db)

    # It is an ordinary draft again.
    assert bank.facts.confirm(draft.id, expected_version=restored.version).status == (
        FactStatus.CONFIRMED
    )


def test_archive_and_restore(bank: Bank, db: Database) -> None:
    fact = typed(bank, NEWSLETTER)
    versions_before = version_rows(db, fact.id)

    archived = bank.facts.archive(fact.id, expected_version=fact.version)

    assert archived.status == FactStatus.ARCHIVED
    assert fact_row(db, fact.id)["status"] == "archived"
    assert event_types(db, fact.id)[-1] == "archived"

    restored = bank.facts.restore(fact.id, expected_version=archived.version)

    assert restored.status == FactStatus.CONFIRMED
    assert fact_row(db, fact.id)["status"] == "confirmed"
    assert event_types(db, fact.id)[-2:] == ["archived", "restored"]
    assert restored.content == fact.content
    assert version_rows(db, fact.id) == versions_before
    assert fact.version < archived.version < restored.version


def _confirmed(bank: Bank, db: Database) -> Any:
    return typed(bank, NEWSLETTER)


def _archived(bank: Bank, db: Database) -> Any:
    fact = typed(bank, NEWSLETTER)
    return bank.facts.archive(fact.id, expected_version=fact.version)


def _discarded(bank: Bank, db: Database) -> Any:
    draft = _draft(bank, db)
    return bank.facts.discard(draft.id, expected_version=draft.version)


Maker = Callable[[Bank, Database], Any]
Move = Callable[[Bank, Any, int], Any]

REFUSED_MOVES: list[Any] = [
    pytest.param(_confirmed, "confirm", id="confirm-a-confirmed-fact"),
    pytest.param(_confirmed, "discard", id="discard-a-confirmed-fact"),
    pytest.param(_confirmed, "restore", id="restore-a-fact-that-is-not-archived"),
    pytest.param(_confirmed, "undo_discard", id="undo-discard-of-a-confirmed-fact"),
    pytest.param(_draft, "archive", id="archive-a-draft"),
    pytest.param(_draft, "restore", id="restore-a-draft"),
    pytest.param(_draft, "undo_discard", id="undo-discard-of-a-draft"),
    pytest.param(_archived, "archive", id="archive-twice"),
    pytest.param(_archived, "confirm", id="confirm-an-archived-fact"),
    pytest.param(_archived, "discard", id="discard-an-archived-fact"),
    pytest.param(_discarded, "confirm", id="confirm-a-discarded-draft"),
    pytest.param(_discarded, "discard", id="discard-twice"),
    pytest.param(_discarded, "archive", id="archive-a-discarded-draft"),
]


@pytest.mark.parametrize(("make", "move"), REFUSED_MOVES)
def test_moves_the_lifecycle_refuses_raise_and_write_nothing(
    bank: Bank, db: Database, make: Maker, move: str
) -> None:
    from app.domain.errors import FactTransitionRefused

    fact = make(bank, db)
    before = snapshot(db)

    with pytest.raises(FactTransitionRefused) as refused:
        getattr(bank.facts, move)(fact.id, expected_version=fact.version)

    assert refused.value.kind == "FactTransitionRefused"
    assert snapshot(db) == before


def test_edit_and_confirm_of_a_confirmed_fact_refused_and_writes_nothing(
    bank: Bank, db: Database
) -> None:
    from app.domain.errors import FactTransitionRefused

    fact = typed(bank, NEWSLETTER)
    before = snapshot(db)
    with pytest.raises(FactTransitionRefused):
        bank.facts.edit_and_confirm(fact.id, _text(CLOSE), expected_version=fact.version)
    assert snapshot(db) == before


# --- optimistic concurrency ------------------------------------------------------

STALE_WRITES: list[Any] = [
    pytest.param(
        _confirmed, lambda b, f, v: b.facts.edit(f.id, _text(CLOSE), expected_version=v), id="edit"
    ),
    pytest.param(_draft, lambda b, f, v: b.facts.confirm(f.id, expected_version=v), id="confirm"),
    pytest.param(
        _draft,
        lambda b, f, v: b.facts.edit_and_confirm(f.id, _text(CLOSE), expected_version=v),
        id="edit-and-confirm",
    ),
    pytest.param(_draft, lambda b, f, v: b.facts.discard(f.id, expected_version=v), id="discard"),
    pytest.param(
        _discarded,
        lambda b, f, v: b.facts.undo_discard(f.id, expected_version=v),
        id="undo-discard",
    ),
    pytest.param(
        _confirmed, lambda b, f, v: b.facts.archive(f.id, expected_version=v), id="archive"
    ),
    pytest.param(
        _archived, lambda b, f, v: b.facts.restore(f.id, expected_version=v), id="restore"
    ),
    pytest.param(
        _confirmed,
        lambda b, f, v: b.facts.set_privacy(f.id, Privacy.NEVER_USE, expected_version=v),
        id="privacy",
    ),
]


@pytest.mark.parametrize(("make", "write"), STALE_WRITES)
@pytest.mark.parametrize("offset", [-1, 1], ids=["older", "newer"])
def test_stale_version_raises_conflict_and_writes_nothing(
    bank: Bank, db: Database, make: Maker, write: Move, offset: int
) -> None:
    fact = make(bank, db)
    before = snapshot(db)

    with pytest.raises(VersionConflict):
        write(bank, fact, fact.version + offset)

    assert snapshot(db) == before
    assert not db.connection.in_transaction
    # The same write with the version the caller should have had goes through.
    write(bank, fact, fact.version)
    assert snapshot(db) != before


def test_second_writer_with_the_same_version_gets_the_conflict(bank: Bank, db: Database) -> None:
    fact = typed(bank, CLOSE)
    bank.facts.edit(fact.id, _text(CLOSE_V2), expected_version=fact.version)
    after_first = snapshot(db)

    with pytest.raises(VersionConflict) as conflict:
        bank.facts.edit(fact.id, _text(CLOSE_V3), expected_version=fact.version)

    assert conflict.value.user_message == (
        "This item changed elsewhere. Reload to see the latest version."
    )
    assert snapshot(db) == after_first
    assert current_version_row(db, fact.id)["text"] == CLOSE_V2


# --- privacy ---------------------------------------------------------------------


def test_privacy_change_recorded_as_event(bank: Bank, db: Database, clock: FrozenClock) -> None:
    fact = typed(bank, CLOSE)
    clock.advance(timedelta(minutes=2))

    hidden = bank.facts.set_privacy(fact.id, Privacy.NEVER_USE, expected_version=fact.version)

    assert hidden.privacy == Privacy.NEVER_USE
    assert hidden.version > fact.version
    assert fact_row(db, fact.id)["privacy"] == "never_use"
    # Privacy lives on the fact: no version for it (ADR-090 item 4).
    assert hidden.content_version == 1
    assert len(version_rows(db, fact.id)) == 1
    event = event_rows(db, fact.id)[-1]
    assert event["type"] == "privacy_changed"
    assert json.loads(event["detail"]) == {"from": "normal", "to": "never_use"}
    assert _at(event["created_at"]) == START + timedelta(minutes=2)

    general = bank.facts.set_privacy(fact.id, Privacy.GENERALIZE, expected_version=hidden.version)

    assert general.privacy == Privacy.GENERALIZE
    assert fact_row(db, fact.id)["privacy"] == "generalize"
    assert json.loads(event_rows(db, fact.id)[-1]["detail"]) == {
        "from": "never_use",
        "to": "generalize",
    }
    assert event_types(db, fact.id).count("privacy_changed") == 2
    # Never the fact's wording in an event.
    for recorded in event_rows(db, fact.id):
        assert CLOSE not in (recorded["detail"] or "")


def test_privacy_can_change_in_any_status(bank: Bank, db: Database) -> None:
    draft = _draft(bank, db)
    hidden = bank.facts.set_privacy(draft.id, Privacy.NEVER_USE, expected_version=draft.version)
    assert (hidden.status, hidden.privacy) == (FactStatus.DRAFT, Privacy.NEVER_USE)

    kept = bank.facts.confirm(draft.id, expected_version=hidden.version)
    assert (kept.status, kept.privacy) == (FactStatus.CONFIRMED, Privacy.NEVER_USE)


# --- unknown ids, numbers, atomic writes -----------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda b: b.facts.get("nope"), id="get"),
        pytest.param(lambda b: b.facts.versions("nope"), id="versions"),
        pytest.param(lambda b: b.facts.confirm("nope", expected_version=1), id="confirm"),
        pytest.param(lambda b: b.facts.edit("nope", _text(CLOSE), expected_version=1), id="edit"),
        pytest.param(lambda b: b.facts.archive("nope", expected_version=1), id="archive"),
        pytest.param(
            lambda b: b.facts.set_privacy("nope", Privacy.NEVER_USE, expected_version=1),
            id="privacy",
        ),
    ],
)
def test_unknown_fact_raises_not_found_and_writes_nothing(
    bank: Bank, db: Database, call: Callable[[Bank], Any]
) -> None:
    from app.domain.errors import FactNotFound

    typed(bank, CLOSE)
    before = snapshot(db)
    with pytest.raises(FactNotFound):
        call(bank)
    assert snapshot(db) == before


def test_numbers_count_up_and_are_never_reused(bank: Bank, db: Database) -> None:
    first = typed(bank, CLOSE)
    add_import(db, "imp-0001")
    drafts = import_drafts(bank, "imp-0001", [STOCKTAKE, NEWSLETTER])
    bank.facts.discard(drafts[1].id, expected_version=drafts[1].version)
    later = typed(bank, "Negotiated a new courier contract")

    assert [first.number, drafts[0].number, drafts[1].number, later.number] == [1, 2, 3, 4]
    # An edit never renumbers a fact.
    edited = bank.facts.edit(first.id, _text(CLOSE_V2), expected_version=first.version)
    assert edited.number == 1


def test_numbers_are_assigned_inside_the_write_across_connections(
    bank: Bank, db: Database, clock: FrozenClock
) -> None:
    # ADR-090: a number remembered by a service would collide with a fact written through
    # another connection. The writes here take turns, so this doesn't show a race between
    # two open transactions.
    other_db = second_connection(db)
    try:
        other = make_bank(other_db, clock, SequentialIdFactory("other"))
        numbers = [
            typed(bank, CLOSE).number,
            typed(other, STOCKTAKE).number,
            typed(bank, NEWSLETTER).number,
            typed(other, "Negotiated a new courier contract").number,
            typed(other, "Wrote the depot's fire drill plan").number,
            typed(bank, "Kept the petty cash ledger").number,
        ]
    finally:
        other_db.close()

    assert numbers == [1, 2, 3, 4, 5, 6]
    assert count(db, "facts") == 6


def test_create_is_one_transaction(bank: Bank, db: Database, clock: FrozenClock) -> None:
    role = bank.roles.create(employer="Lanternfish Logistics", title="Operations Analyst")
    assert_atomic(
        db,
        clock,
        lambda fresh: typed(
            fresh, CLOSE, role_id=role.id, skills=("Reconciliation", "Forecasting")
        ),
    )
    assert count(db, "facts") == 1
    assert count(db, "skills") == 2


def test_create_drafts_is_one_transaction(bank: Bank, db: Database, clock: FrozenClock) -> None:
    add_import(db, "imp-0001")
    assert_atomic(
        db,
        clock,
        lambda fresh: import_drafts(
            fresh,
            "imp-0001",
            [STOCKTAKE, NEWSLETTER, CLOSE],
            role=("Lanternfish Logistics", "Operations Analyst"),
            skills=("Inventory control",),
        ),
    )
    assert count(db, "facts") == 3
    assert count(db, "roles") == 1
