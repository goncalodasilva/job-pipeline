"""Review fixes to the fact bank services (M3.3; ADR-089 items 8 and 11, ADR-090).

- A merge that takes a version's wording as it stands (the draft's or the target's, compared
  after ``FactContent.normalized()``) keeps that version's provider, model, prompt and
  ``needs_check`` on the target's new version; wording the user wrote carries none. The view
  reports ``needs_check`` only while the fact is a draft (``test_fact_needs_check.py``).
- ``FactView.origin_provider`` and ``origin_model`` come from the fact's earliest version a
  provider made, so a fact stays recognisable as AI-made after a light edit.
- Merges into one target can be undone newest first; undo is refused once the target was
  reworded, and leaves the skills alone once the user saved the target's details.
- Branches that were reachable but untested, and friendly errors for an unknown type or status.

What was written is checked with SQL. Real migrated SQLite in a temp folder, ``FrozenClock``
and ``SequentialIdFactory``. Application modules are imported inside the tests so each fails
on its own while they are missing. All data is fictional.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from app.domain.facts import FactContent, FactStatus, Privacy
from app.modules.storage import Database
from tests.fact_bank import (
    MODEL,
    PROMPT_ID,
    PROMPT_VERSION,
    PROVIDER,
    STAMP,
    START,
    Bank,
    add_import,
    count,
    current_version_row,
    event_types,
    fact_row,
    import_drafts,
    make_bank,
    open_migrated,
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
SECOND_DRAFT_TEXT = "Shortened the month-end close with a shared checklist"
SECOND_MERGED_TEXT = "Cut month-end close from nine days to four with a shared checklist"

OTHER_PROVIDER = "fake-other"
OTHER_MODEL = "fake-quick-1"
OTHER_PROMPT_ID = "session_answer"
OTHER_PROMPT_VERSION = "2"

ZERO_WIDTH_SPACE = chr(0x200B)


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


def _other_draft(bank: Bank, text: str, *, needs_check: bool = False) -> Any:
    """A draft another (fictional) provider made, outside any import."""
    from app.services.facts import DraftInput, DraftOrigin

    origin = DraftOrigin(
        source="session",
        provider=OTHER_PROVIDER,
        model=OTHER_MODEL,
        prompt_id=OTHER_PROMPT_ID,
        prompt_version=OTHER_PROMPT_VERSION,
    )
    (draft,) = bank.facts.create_drafts(origin, [DraftInput(text=text, needs_check=needs_check)])
    return draft


def _merge(bank: Bank, draft_id: str, target_id: str, text: str) -> Any:
    return bank.facts.merge(
        draft_id,
        target_id,
        FactContent(text=text),
        expected_draft_version=bank.facts.get(draft_id).version,
        expected_target_version=bank.facts.get(target_id).version,
    )


def _undo_merge(bank: Bank, draft_id: str, target_id: str) -> Any:
    return bank.facts.undo_merge(
        draft_id,
        expected_draft_version=bank.facts.get(draft_id).version,
        expected_target_version=bank.facts.get(target_id).version,
    )


def _provenance(row: dict[str, Any]) -> tuple[object, ...]:
    return (
        row["provider"],
        row["model"],
        row["prompt_id"],
        row["prompt_version"],
        row["needs_check"],
    )


# --- provenance through a merge --------------------------------------------------


def test_merge_taking_the_drafts_wording_keeps_the_drafts_provenance(
    bank: Bank, db: Database
) -> None:
    target = typed(bank, TARGET_TEXT)
    draft = _other_draft(bank, DRAFT_TEXT, needs_check=True)

    # As a form posts it: the same wording with other line endings and edge whitespace.
    merged = _merge(bank, draft.id, target.id, f"  {DRAFT_TEXT}\r\n")

    version = current_version_row(db, target.id)
    assert (version["version"], version["source"], version["text"]) == (2, "merge", DRAFT_TEXT)
    assert _provenance(version) == (
        OTHER_PROVIDER,
        OTHER_MODEL,
        OTHER_PROMPT_ID,
        OTHER_PROMPT_VERSION,
        1,
    )
    # The row keeps the flag; a confirmed fact's view never reports it.
    assert (merged.provider, merged.model, merged.needs_check) == (
        OTHER_PROVIDER,
        OTHER_MODEL,
        False,
    )
    # The target's first version is the user's own, as before.
    assert _provenance(version_rows(db, target.id)[0]) == (None, None, None, None, 0)


def test_merge_keeping_the_targets_wording_keeps_the_targets_provenance(
    bank: Bank, db: Database
) -> None:
    add_import(db, "imp-0001")
    (target,) = import_drafts(bank, "imp-0001", [TARGET_TEXT])
    # Merge targets are confirmed facts only (user decision, 2026-10-16).
    target = bank.facts.confirm(target.id, expected_version=target.version)
    draft = _other_draft(bank, DRAFT_TEXT, needs_check=True)

    merged = _merge(bank, draft.id, target.id, TARGET_TEXT)

    version = current_version_row(db, target.id)
    assert (version["version"], version["source"]) == (2, "merge")
    assert _provenance(version) == (PROVIDER, MODEL, PROMPT_ID, PROMPT_VERSION, 0)
    assert (merged.provider, merged.model, merged.needs_check) == (PROVIDER, MODEL, False)


def test_merge_keeping_a_typed_targets_wording_stays_unlabelled(bank: Bank, db: Database) -> None:
    target = typed(bank, TARGET_TEXT)
    draft = _other_draft(bank, DRAFT_TEXT, needs_check=True)

    _merge(bank, draft.id, target.id, TARGET_TEXT)

    assert _provenance(current_version_row(db, target.id)) == (None, None, None, None, 0)


def test_merge_with_wording_the_user_wrote_carries_no_provenance(bank: Bank, db: Database) -> None:
    add_import(db, "imp-0001")
    (target,) = import_drafts(bank, "imp-0001", [TARGET_TEXT])
    # Merge targets are confirmed facts only (user decision, 2026-10-16).
    target = bank.facts.confirm(target.id, expected_version=target.version)
    draft = _other_draft(bank, DRAFT_TEXT, needs_check=True)

    merged = _merge(bank, draft.id, target.id, MERGED_TEXT)

    version = current_version_row(db, target.id)
    assert (version["source"], version["text"]) == ("merge", MERGED_TEXT)
    assert _provenance(version) == (None, None, None, None, 0)
    assert (merged.provider, merged.model, merged.needs_check) == (None, None, False)


# --- where a fact came from ------------------------------------------------------


def test_ai_draft_edited_by_the_user_still_reports_its_origin(bank: Bank, db: Database) -> None:
    add_import(db, "imp-0001")
    (draft,) = import_drafts(bank, "imp-0001", [DRAFT_TEXT])
    assert (draft.origin_provider, draft.origin_model) == (PROVIDER, MODEL)

    edited = bank.facts.edit(draft.id, _text(MERGED_TEXT), expected_version=draft.version)
    kept = bank.facts.confirm(draft.id, expected_version=edited.version)

    for view in (edited, kept, bank.facts.get(draft.id)):
        # The current wording is the user's; the fact still began with the provider.
        assert (view.provider, view.model) == (None, None)
        assert (view.origin_provider, view.origin_model) == (PROVIDER, MODEL)


def test_typed_fact_reports_no_origin(bank: Bank) -> None:
    fact = typed(bank, TARGET_TEXT)
    edited = bank.facts.edit(fact.id, _text(MERGED_TEXT), expected_version=fact.version)

    for view in (fact, edited, bank.facts.get(fact.id)):
        assert (view.origin_provider, view.origin_model) == (None, None)


def test_typed_fact_that_took_ai_wording_by_merge_reports_that_origin(bank: Bank) -> None:
    target = typed(bank, TARGET_TEXT)
    draft = _other_draft(bank, DRAFT_TEXT)

    merged = _merge(bank, draft.id, target.id, DRAFT_TEXT)

    assert (merged.origin_provider, merged.origin_model) == (OTHER_PROVIDER, OTHER_MODEL)
    # A light edit afterwards doesn't lose it.
    edited = bank.facts.edit(target.id, _text(MERGED_TEXT), expected_version=merged.version)
    assert (edited.provider, edited.model) == (None, None)
    assert (edited.origin_provider, edited.origin_model) == (OTHER_PROVIDER, OTHER_MODEL)


def test_library_results_carry_the_origin(bank: Bank, db: Database) -> None:
    from app.services.fact_library import LibraryQuery

    add_import(db, "imp-0001")
    (draft,) = import_drafts(bank, "imp-0001", [DRAFT_TEXT])
    edited = bank.facts.edit(draft.id, _text(MERGED_TEXT), expected_version=draft.version)
    bank.facts.confirm(draft.id, expected_version=edited.version)
    typed(bank, TARGET_TEXT)

    page = bank.library.search(LibraryQuery())

    assert {item.content.text: item.origin_provider for item in page.items} == {
        MERGED_TEXT: PROVIDER,
        TARGET_TEXT: None,
    }


# --- undoing merges --------------------------------------------------------------


def test_two_merges_into_one_target_are_undone_newest_first(bank: Bank, db: Database) -> None:
    add_import(db, "imp-0001")
    target = typed(bank, TARGET_TEXT, skills=("Reconciliation",))
    (first,) = import_drafts(bank, "imp-0001", [DRAFT_TEXT], skills=("Process automation",))
    (second,) = import_drafts(bank, "imp-0001", [SECOND_DRAFT_TEXT], skills=("Checklists",))
    _merge(bank, first.id, target.id, MERGED_TEXT)
    _merge(bank, second.id, target.id, SECOND_MERGED_TEXT)
    assert skill_names_of(db, target.id) == {"Reconciliation", "Process automation", "Checklists"}

    reopened_second = _undo_merge(bank, second.id, target.id)

    assert reopened_second.status == FactStatus.DRAFT
    assert bank.facts.get(target.id).content == FactContent(text=MERGED_TEXT)
    assert skill_names_of(db, target.id) == {"Reconciliation", "Process automation"}

    reopened_first = _undo_merge(bank, first.id, target.id)

    assert reopened_first.status == FactStatus.DRAFT
    after = bank.facts.get(target.id)
    assert after.content == FactContent(text=TARGET_TEXT)
    assert after.status == FactStatus.CONFIRMED
    assert skill_names_of(db, target.id) == {"Reconciliation"}
    assert [
        (row["version"], row["text"], row["source"]) for row in version_rows(db, target.id)
    ] == [
        (1, TARGET_TEXT, "manual"),
        (2, MERGED_TEXT, "merge"),
        (3, SECOND_MERGED_TEXT, "merge"),
        (4, MERGED_TEXT, "merge_undo"),
        (5, TARGET_TEXT, "merge_undo"),
    ]
    for draft, text in ((first, DRAFT_TEXT), (second, SECOND_DRAFT_TEXT)):
        row = fact_row(db, draft.id)
        assert (row["status"], row["merged_into_fact_id"]) == ("draft", None)
        assert bank.facts.get(draft.id).content == FactContent(text=text)
        assert event_types(db, draft.id) == ["created", "merged_into", "merge_undone"]
    assert skill_names_of(db, first.id) == {"Process automation"}
    assert skill_names_of(db, second.id) == {"Checklists"}


def test_merge_undo_out_of_order_refused_while_a_later_merge_stands(
    bank: Bank, db: Database
) -> None:
    from app.domain.errors import FactTransitionRefused

    add_import(db, "imp-0001")
    target = typed(bank, TARGET_TEXT)
    first, second = import_drafts(bank, "imp-0001", [DRAFT_TEXT, SECOND_DRAFT_TEXT])
    _merge(bank, first.id, target.id, MERGED_TEXT)
    _merge(bank, second.id, target.id, SECOND_MERGED_TEXT)
    before = snapshot(db)

    with pytest.raises(FactTransitionRefused):
        _undo_merge(bank, first.id, target.id)
    assert snapshot(db) == before


def test_merge_undo_refused_once_the_target_was_reworded(bank: Bank, db: Database) -> None:
    from app.domain.errors import FactTransitionRefused

    add_import(db, "imp-0001")
    target = typed(bank, TARGET_TEXT, skills=("Reconciliation",))
    (draft,) = import_drafts(bank, "imp-0001", [DRAFT_TEXT], skills=("Process automation",))
    merged = _merge(bank, draft.id, target.id, MERGED_TEXT)
    bank.facts.edit(
        target.id,
        _text("Cut month-end close to four days", skills=merged.skills),
        expected_version=merged.version,
    )
    before = snapshot(db)

    # With the versions as they are now: this is the guard, not a stale page.
    with pytest.raises(FactTransitionRefused) as refused:
        _undo_merge(bank, draft.id, target.id)

    assert refused.value.kind == "FactTransitionRefused"
    assert snapshot(db) == before
    assert not db.connection.in_transaction
    assert fact_row(db, draft.id)["merged_into_fact_id"] == target.id


def test_merge_undo_keeps_skills_the_user_saved_after_the_merge(bank: Bank, db: Database) -> None:
    add_import(db, "imp-0001")
    target = typed(bank, TARGET_TEXT, skills=("Reconciliation", "Reporting"))
    (draft,) = import_drafts(bank, "imp-0001", [DRAFT_TEXT], skills=("Process automation",))
    merged = _merge(bank, draft.id, target.id, MERGED_TEXT)
    assert merged.skills == ("Process automation", "Reconciliation", "Reporting")
    # The user saves the skill list on purpose: the merged-in skill stays, another goes.
    saved = bank.facts.edit(
        target.id,
        _text(MERGED_TEXT, skills=("Process automation", "Reconciliation")),
        expected_version=merged.version,
    )
    assert event_types(db, target.id)[-2:] == ["merged", "details_changed"]
    assert saved.content_version == merged.content_version

    reopened = _undo_merge(bank, draft.id, target.id)

    assert reopened.status == FactStatus.DRAFT
    after = bank.facts.get(target.id)
    # The wording goes back; the skills are what the user saved.
    assert after.content == FactContent(text=TARGET_TEXT)
    assert skill_names_of(db, target.id) == {"Process automation", "Reconciliation"}
    assert skill_names_of(db, draft.id) == {"Process automation"}


def test_merge_undo_still_removes_skills_saved_before_the_merge(bank: Bank, db: Database) -> None:
    add_import(db, "imp-0001")
    target = typed(bank, TARGET_TEXT, skills=("Reconciliation",))
    # A details change from before the merge says nothing about the merged-in skills.
    target = bank.facts.edit(
        target.id,
        _text(TARGET_TEXT, skills=("Reconciliation", "Reporting")),
        expected_version=target.version,
    )
    (draft,) = import_drafts(bank, "imp-0001", [DRAFT_TEXT], skills=("Process automation",))
    _merge(bank, draft.id, target.id, MERGED_TEXT)

    _undo_merge(bank, draft.id, target.id)

    assert skill_names_of(db, target.id) == {"Reconciliation", "Reporting"}


# --- branches that were reachable but untested -----------------------------------


def _discarded(bank: Bank, db: Database) -> Any:
    add_import(db, "imp-0001")
    (draft,) = import_drafts(bank, "imp-0001", [DRAFT_TEXT])
    return bank.facts.discard(draft.id, expected_version=draft.version)


def _closed_by_merge(bank: Bank, db: Database) -> Any:
    add_import(db, "imp-0001")
    target = typed(bank, TARGET_TEXT)
    (draft,) = import_drafts(bank, "imp-0001", [DRAFT_TEXT])
    _merge(bank, draft.id, target.id, MERGED_TEXT)
    return bank.facts.get(draft.id)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(_discarded, id="discarded"),
        pytest.param(_closed_by_merge, id="closed-by-merge"),
    ],
)
@pytest.mark.parametrize("confirm", [False, True], ids=["edit", "edit-and-confirm"])
def test_editing_a_discarded_fact_refused_and_writes_nothing(
    bank: Bank, db: Database, make: Callable[[Bank, Database], Any], confirm: bool
) -> None:
    from app.domain.errors import FactTransitionRefused

    gone = make(bank, db)
    assert gone.status == FactStatus.DISCARDED
    before = snapshot(db)
    write = bank.facts.edit_and_confirm if confirm else bank.facts.edit

    with pytest.raises(FactTransitionRefused) as refused:
        write(gone.id, _text(MERGED_TEXT, skills=("Forecasting",)), expected_version=gone.version)

    assert refused.value.kind == "FactTransitionRefused"
    assert snapshot(db) == before
    assert not db.connection.in_transaction


def test_privacy_set_to_the_same_value_writes_nothing(bank: Bank, db: Database) -> None:
    fact = typed(bank, TARGET_TEXT)
    hidden = bank.facts.set_privacy(fact.id, Privacy.NEVER_USE, expected_version=fact.version)
    before = snapshot(db)

    again = bank.facts.set_privacy(fact.id, Privacy.NEVER_USE, expected_version=hidden.version)

    assert again == hidden
    assert again.version == hidden.version
    assert snapshot(db) == before
    assert event_types(db, fact.id).count("privacy_changed") == 1


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param("forecasting", id="its-name-in-other-case"),
        pytest.param("Demand  PLANNING", id="an-alias-it-has"),
    ],
)
def test_alias_the_skill_already_has_changes_nothing(
    bank: Bank, db: Database, spelling: str
) -> None:
    skill = bank.skills.add("Forecasting", aliases=("Demand planning",))
    before = snapshot(db)

    same = bank.skills.add_alias(skill.id, spelling, expected_version=skill.version)

    assert same == skill
    assert snapshot(db) == before
    assert bank.skills.list() == (skill,)


def test_alias_for_an_unknown_skill_raises_not_found_and_writes_nothing(
    bank: Bank, db: Database
) -> None:
    from app.domain.errors import FactNotFound

    bank.skills.add("Forecasting")
    before = snapshot(db)

    with pytest.raises(FactNotFound):
        bank.skills.add_alias("no-such-skill", "Demand planning", expected_version=1)

    assert snapshot(db) == before
    assert not db.connection.in_transaction


@pytest.mark.parametrize(
    "blank", ["", "   ", "\t\n", ZERO_WIDTH_SPACE], ids=["empty", "spaces", "tab-newline", "zwsp"]
)
def test_blank_name_among_a_facts_skills_is_skipped(bank: Bank, db: Database, blank: str) -> None:
    fact = typed(bank, TARGET_TEXT, skills=("Forecasting", blank, "Reporting"))

    assert fact.skills == ("Forecasting", "Reporting")
    assert count(db, "skills") == 2

    edited = bank.facts.edit(
        fact.id, _text(TARGET_TEXT, skills=(blank, "Reporting")), expected_version=fact.version
    )
    assert edited.skills == ("Reporting",)
    assert count(db, "skills") == 2


def test_skill_name_with_invisible_characters_is_the_same_skill(bank: Bank, db: Database) -> None:
    skill = bank.skills.add("Python")

    fact = typed(bank, TARGET_TEXT, skills=("Python" + ZERO_WIDTH_SPACE,))

    assert fact.skills == ("Python",)
    assert bank.skills.find("Py" + ZERO_WIDTH_SPACE + "thon") == skill
    assert count(db, "skills") == 1
    # A new skill is stored without them.
    added = bank.skills.add("Fore" + ZERO_WIDTH_SPACE + "casting")
    assert added.name == "Forecasting"


def test_update_fact_with_a_stale_version_changes_nothing(bank: Bank, db: Database) -> None:
    from app.modules.storage import facts_repo

    fact = typed(bank, TARGET_TEXT)
    row = fact_row(db, fact.id)
    before = snapshot(db)

    def update(fact_id: str, expected_version: int) -> bool:
        with db.transaction() as conn:
            return facts_repo.update_fact(
                conn,
                fact_id,
                expected_version=expected_version,
                type="story",
                status="archived",
                role_id=None,
                privacy="never_use",
                tags='["changed"]',
                start_date="2024",
                end_date="2025",
                current_version_id=row["current_version_id"],
                merged_into_fact_id=None,
                now=STAMP,
            )

    assert update(fact.id, fact.version + 1) is False
    assert update(fact.id, fact.version - 1) is False
    assert update("no-such-fact", fact.version) is False
    assert snapshot(db) == before

    # The same write with the version the row has goes through and bumps it.
    assert update(fact.id, fact.version) is True
    after = fact_row(db, fact.id)
    assert (after["status"], after["privacy"], after["version"]) == (
        "archived",
        "never_use",
        fact.version + 1,
    )


# --- friendly errors -------------------------------------------------------------


def test_unknown_fact_type_refused_and_writes_nothing(bank: Bank, db: Database) -> None:
    from app.domain.errors import FactInputInvalid
    from app.services.facts import DraftInput, DraftOrigin

    fact = typed(bank, TARGET_TEXT)
    before = snapshot(db)
    attempts: list[Callable[[], object]] = [
        lambda: bank.facts.create(_text(DRAFT_TEXT, type="anecdote")),
        lambda: bank.facts.edit(
            fact.id, _text(TARGET_TEXT, type="anecdote"), expected_version=fact.version
        ),
        lambda: bank.facts.create_drafts(
            DraftOrigin(source="session"),
            [DraftInput(text=DRAFT_TEXT, type="anecdote")],  # type: ignore[arg-type]
        ),
    ]

    for attempt in attempts:
        with pytest.raises(FactInputInvalid) as refused:
            attempt()
        assert refused.value.field == "type"
        assert refused.value.kind == "FactInputInvalid"
        assert refused.value.user_message != FactInputInvalid.message
    assert snapshot(db) == before
    assert not db.connection.in_transaction


def test_fact_type_given_as_its_stored_word_is_accepted(bank: Bank) -> None:
    from app.domain.facts import FactType

    story = bank.facts.create(_text(TARGET_TEXT, type="story"))

    assert story.type == FactType.STORY


def test_unknown_library_status_refused(bank: Bank) -> None:
    from app.domain.errors import FactInputInvalid
    from app.services.fact_library import LibraryQuery

    typed(bank, TARGET_TEXT)

    with pytest.raises(FactInputInvalid) as refused:
        bank.library.search(LibraryQuery(status="published"))  # type: ignore[arg-type]

    assert refused.value.field == "status"
    assert refused.value.user_message != FactInputInvalid.message
    # The stored word of a real status is accepted.
    assert bank.library.search(LibraryQuery(status="confirmed")).total == 1  # type: ignore[arg-type]


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"], ids=["empty", "spaces", "tab-newline"])
def test_blank_role_id_means_no_role(bank: Bank, db: Database, blank: str) -> None:
    role = bank.roles.create(employer="Lanternfish Logistics", title="Operations Analyst")

    fact = typed(bank, TARGET_TEXT, role_id=blank)

    assert fact.role_id is None
    assert fact_row(db, fact.id)["role_id"] is None

    with_role = bank.facts.edit(
        fact.id, _text(TARGET_TEXT, role_id=role.id), expected_version=fact.version
    )
    assert with_role.role_id == role.id
    # The editor's "no role" choice posts a blank value: the role is taken off.
    cleared = bank.facts.edit(
        fact.id, _text(TARGET_TEXT, role_id=blank), expected_version=with_role.version
    )
    assert cleared.role_id is None
    assert fact_row(db, fact.id)["role_id"] is None
