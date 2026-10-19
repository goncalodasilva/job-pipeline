"""Roles and skills of the fact bank (M3.3; ADR-089 item 11, ADR-090 items 4 and 9).

``app.services.roles.RoleService(db, clock, ids)``:

- ``create(*, employer, title, start_date=None, end_date=None) -> RoleView``: a role the user
  types is confirmed; it needs an employer or a title and real dates.
- ``get(role_id) -> RoleView`` and ``list() -> tuple[RoleView, ...]``.
- A draft role (made by ``FactService.create_drafts``) is confirmed in the same transaction as
  the first of its facts to be confirmed.

``app.services.skills.SkillService(db, clock, ids)``:

- ``list() -> tuple[SkillView, ...]``: empty until the user or an import adds a skill.
- ``add(name, *, aliases=()) -> SkillView``: returns the skill that already has this name or
  alias (after normalization) instead of making a second one.
- ``find(name) -> SkillView | None``: by name or alias.
- ``add_alias(skill_id, alias, *, expected_version) -> SkillView``: ``SkillAliasTaken`` when the
  alias is another skill's name or alias.

A fact's ``skills`` are names: each is resolved to the skill it names (by name or alias) or
added to the list, inside the fact's own transaction.

``RoleView``: ``id``, ``employer``, ``title``, ``start_date``, ``end_date``, ``status``,
``version``. ``SkillView``: ``id``, ``name``, ``aliases``, ``version``.

Real migrated SQLite in a temp folder, ``FrozenClock`` and ``SequentialIdFactory``. Application
modules are imported inside the tests so each fails on its own while they are missing. All data
is fictional.
"""

import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import VersionConflict
from app.domain.facts import FactStatus, RoleStatus
from app.modules.storage import Database
from tests.fact_bank import (
    START,
    Bank,
    add_import,
    count,
    event_types,
    fact_row,
    import_drafts,
    make_bank,
    open_migrated,
    query,
    role_row,
    skill_names_of,
    snapshot,
    typed,
    version_rows,
)
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

pytestmark = pytest.mark.integration

EMPLOYER = "Lanternfish Logistics"
TITLE = "Operations Analyst"


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


def _skill_rows(db: Database) -> list[dict[str, Any]]:
    return query(db, "SELECT * FROM skills ORDER BY rowid")


# --- skills ----------------------------------------------------------------------


def test_skills_list_starts_empty(bank: Bank, db: Database) -> None:
    assert bank.skills.list() == ()
    assert count(db, "skills") == 0
    assert bank.skills.find("Forecasting") is None


def test_added_skill_is_listed_with_its_name_as_typed(
    bank: Bank, db: Database, clock: FrozenClock
) -> None:
    from app.domain.skills import normalize_skill

    skill = bank.skills.add("  Demand  Forecasting ", aliases=("S&OP",))

    assert skill.name == "Demand Forecasting"
    assert skill.aliases == ("S&OP",)
    assert bank.skills.list() == (skill,)
    row = _skill_rows(db)[0]
    assert row["id"] == skill.id
    assert row["name"] == "Demand Forecasting"
    assert row["normalized_name"] == normalize_skill("Demand Forecasting")
    assert json.loads(row["aliases"]) == ["S&OP"]
    assert row["version"] == skill.version
    assert datetime.fromisoformat(row["created_at"]) == clock.now()


def test_same_skill_in_other_case_not_duplicated(bank: Bank, db: Database) -> None:
    first = bank.skills.add("Forecasting")
    again = bank.skills.add("FORECASTING")
    spaced = bank.skills.add("  forecasting ")

    assert again == first
    assert spaced == first
    assert count(db, "skills") == 1
    # The spelling the user chose first is the one shown.
    assert [skill.name for skill in bank.skills.list()] == ["Forecasting"]

    # The same holds when facts name the skill.
    one = typed(bank, "Built the weekly demand forecast", skills=("forecasting",))
    two = typed(bank, "Forecast courier volumes for peak season", skills=("FORECASTING",))
    assert count(db, "skills") == 1
    assert one.skills == ("Forecasting",)
    assert two.skills == ("Forecasting",)
    assert skill_names_of(db, one.id) == {"Forecasting"}
    assert skill_names_of(db, two.id) == {"Forecasting"}
    links = query(db, "SELECT skill_id FROM fact_skills")
    assert {row["skill_id"] for row in links} == {first.id}


def test_one_fact_naming_a_skill_twice_links_it_once(bank: Bank, db: Database) -> None:
    fact = typed(bank, "Built the weekly demand forecast", skills=("Forecasting", "forecasting "))

    assert fact.skills == ("Forecasting",)
    assert count(db, "skills") == 1
    assert count(db, "fact_skills") == 1


def test_alias_resolves_to_the_canonical_skill(bank: Bank, db: Database) -> None:
    skill = bank.skills.add("Forecasting", aliases=("Demand planning",))

    assert bank.skills.find("demand  PLANNING") == skill
    assert bank.skills.find("forecasting") == skill
    assert bank.skills.find("Planning") is None
    # Adding the alias as if it were a new skill gives the skill it already names.
    assert bank.skills.add("Demand Planning") == skill
    assert count(db, "skills") == 1

    fact = typed(bank, "Planned stock for the winter peak", skills=("demand planning",))
    assert fact.skills == ("Forecasting",)
    assert skill_names_of(db, fact.id) == {"Forecasting"}
    assert count(db, "skills") == 1


def test_alias_added_later_matches_too(bank: Bank, db: Database, clock: FrozenClock) -> None:
    skill = bank.skills.add("Forecasting")
    clock.advance(timedelta(minutes=1))

    updated = bank.skills.add_alias(skill.id, " Demand planning ", expected_version=skill.version)

    assert updated.aliases == ("Demand planning",)
    assert updated.version > skill.version
    assert bank.skills.find("DEMAND PLANNING") == updated
    row = _skill_rows(db)[0]
    assert json.loads(row["aliases"]) == ["Demand planning"]
    assert datetime.fromisoformat(row["updated_at"]) == START + timedelta(minutes=1)
    assert datetime.fromisoformat(row["created_at"]) == START


@pytest.mark.parametrize(
    "alias",
    [
        pytest.param("Scheduling", id="another-skills-name"),
        pytest.param("  scheduling", id="another-skills-name-in-other-case"),
        pytest.param("ROSTERING", id="another-skills-alias"),
    ],
)
def test_alias_collisions_refused_and_write_nothing(bank: Bank, db: Database, alias: str) -> None:
    from app.domain.errors import SkillAliasTaken

    bank.skills.add("Scheduling", aliases=("Rostering",))
    skill = bank.skills.add("Forecasting")
    before = snapshot(db)

    with pytest.raises(SkillAliasTaken) as refused:
        bank.skills.add_alias(skill.id, alias, expected_version=skill.version)
    assert refused.value.kind == "SkillAliasTaken"
    assert snapshot(db) == before

    # A new skill can't claim it either.
    with pytest.raises(SkillAliasTaken):
        bank.skills.add("Capacity planning", aliases=(alias,))
    assert snapshot(db) == before


def test_alias_with_stale_version_raises_conflict_and_writes_nothing(
    bank: Bank, db: Database
) -> None:
    skill = bank.skills.add("Forecasting")
    bank.skills.add_alias(skill.id, "Demand planning", expected_version=skill.version)
    before = snapshot(db)

    with pytest.raises(VersionConflict):
        bank.skills.add_alias(skill.id, "S&OP", expected_version=skill.version)
    assert snapshot(db) == before


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_skill_names_refused(bank: Bank, db: Database, blank: str) -> None:
    from app.domain.errors import FactInputInvalid

    with pytest.raises(FactInputInvalid):
        bank.skills.add(blank)
    with pytest.raises(FactInputInvalid):
        bank.skills.add("Forecasting", aliases=(blank,))
    assert count(db, "skills") == 0


def test_editing_a_facts_skills_replaces_its_links_without_a_version(
    bank: Bank, db: Database
) -> None:
    fact = typed(bank, "Built the weekly demand forecast", skills=("Forecasting", "Spreadsheets"))
    assert fact.skills == ("Forecasting", "Spreadsheets")

    after = bank.facts.edit(
        fact.id,
        _text("Built the weekly demand forecast", skills=("Forecasting", "Scheduling")),
        expected_version=fact.version,
    )

    assert after.skills == ("Forecasting", "Scheduling")
    assert skill_names_of(db, fact.id) == {"Forecasting", "Scheduling"}
    assert len(version_rows(db, fact.id)) == 1
    assert event_types(db, fact.id)[-1] == "details_changed"
    # A skill stays in the user's list when a fact stops using it.
    assert {skill.name for skill in bank.skills.list()} == {
        "Forecasting",
        "Scheduling",
        "Spreadsheets",
    }
    # The search index follows the links.
    found = query(db, "SELECT fact_id FROM facts_fts WHERE facts_fts MATCH ?", ('"scheduling"',))
    assert [row["fact_id"] for row in found] == [fact.id]
    gone = query(db, "SELECT fact_id FROM facts_fts WHERE facts_fts MATCH ?", ('"spreadsheets"',))
    assert gone == []


# --- roles -----------------------------------------------------------------------


def test_typed_role_is_confirmed(bank: Bank, db: Database, clock: FrozenClock) -> None:
    role = bank.roles.create(
        employer=f"  {EMPLOYER} ", title=TITLE, start_date="2021-09", end_date="2024"
    )

    assert role.status == RoleStatus.CONFIRMED
    assert (role.employer, role.title) == (EMPLOYER, TITLE)
    assert (role.start_date, role.end_date) == ("2021-09", "2024")
    assert bank.roles.get(role.id) == role
    assert bank.roles.list() == (role,)
    row = role_row(db, role.id)
    assert (row["employer"], row["title"], row["status"]) == (EMPLOYER, TITLE, "confirmed")
    assert row["version"] == role.version
    assert datetime.fromisoformat(row["created_at"]) == clock.now()


def test_role_needs_an_employer_or_a_title_and_real_dates(bank: Bank, db: Database) -> None:
    from app.domain.errors import FactInputInvalid

    with pytest.raises(FactInputInvalid):
        bank.roles.create(employer="  ", title="")
    with pytest.raises(FactInputInvalid) as refused:
        bank.roles.create(employer=EMPLOYER, title=TITLE, start_date="2021-13")
    assert refused.value.field == "start_date"
    with pytest.raises(FactInputInvalid) as refused:
        bank.roles.create(employer=EMPLOYER, title=TITLE, end_date="2023-02-29")
    assert refused.value.field == "end_date"
    assert count(db, "roles") == 0

    only_title = bank.roles.create(employer="", title="Volunteer treasurer")
    assert (only_title.employer, only_title.title) == ("", "Volunteer treasurer")


def test_unknown_role_raises_not_found(bank: Bank) -> None:
    from app.domain.errors import FactNotFound

    with pytest.raises(FactNotFound):
        bank.roles.get("nope")


def test_draft_with_impossible_role_dates_writes_nothing(bank: Bank, db: Database) -> None:
    from app.domain.errors import FactInputInvalid
    from app.services.facts import DraftInput, DraftOrigin, DraftRole

    before = snapshot(db)
    with pytest.raises(FactInputInvalid):
        bank.facts.create_drafts(
            DraftOrigin(source="cv_import"),
            [DraftInput(text="Ran the quarterly stocktake", role_key="ops")],
            roles=[DraftRole(key="ops", employer=EMPLOYER, title=TITLE, start_date="2021-00")],
        )
    assert snapshot(db) == before


def test_confirming_fact_confirms_its_draft_role(
    bank: Bank, db: Database, clock: FrozenClock
) -> None:
    add_import(db, "imp-0001")
    first, second = import_drafts(
        bank,
        "imp-0001",
        ["Ran the quarterly stocktake", "Negotiated a new courier contract"],
        role=(EMPLOYER, TITLE),
    )
    (other,) = import_drafts(
        bank,
        "imp-0001",
        ["Kept the petty cash ledger"],
        role=("Brightwater Cooperative", "Volunteer treasurer"),
    )
    role_id, other_role_id = first.role_id, other.role_id
    assert second.role_id == role_id
    assert role_id != other_role_id
    assert role_row(db, role_id)["status"] == "draft"
    draft_role = bank.roles.get(role_id)
    assert draft_role.status == RoleStatus.DRAFT

    # Discarding a draft confirms nothing.
    gone = bank.facts.discard(second.id, expected_version=second.version)
    assert role_row(db, role_id)["status"] == "draft"
    bank.facts.undo_discard(second.id, expected_version=gone.version)
    assert role_row(db, role_id)["status"] == "draft"

    clock.advance(timedelta(minutes=3))
    bank.facts.confirm(first.id, expected_version=first.version)

    row = role_row(db, role_id)
    assert row["status"] == "confirmed"
    assert row["version"] > draft_role.version
    assert datetime.fromisoformat(row["updated_at"]) == START + timedelta(minutes=3)
    assert bank.roles.get(role_id).status == RoleStatus.CONFIRMED
    # Only that role, and only that fact.
    assert role_row(db, other_role_id)["status"] == "draft"
    assert fact_row(db, second.id)["status"] == "draft"
    assert fact_row(db, other.id)["status"] == "draft"


def test_edit_then_confirm_confirms_the_draft_role_too(bank: Bank, db: Database) -> None:
    add_import(db, "imp-0001")
    (draft,) = import_drafts(
        bank, "imp-0001", ["Ran the quarterly stocktake"], role=(EMPLOYER, TITLE)
    )

    done = bank.facts.edit_and_confirm(
        draft.id,
        _text("Ran the quarterly stocktake across three depots", role_id=draft.role_id),
        expected_version=draft.version,
    )

    assert done.status == FactStatus.CONFIRMED
    assert role_row(db, draft.role_id)["status"] == "confirmed"


def test_confirming_a_second_fact_leaves_the_confirmed_role_alone(bank: Bank, db: Database) -> None:
    add_import(db, "imp-0001")
    first, second = import_drafts(
        bank,
        "imp-0001",
        ["Ran the quarterly stocktake", "Negotiated a new courier contract"],
        role=(EMPLOYER, TITLE),
    )
    bank.facts.confirm(first.id, expected_version=first.version)
    role_after_first = role_row(db, first.role_id)

    bank.facts.confirm(second.id, expected_version=second.version)

    assert role_row(db, first.role_id) == role_after_first
