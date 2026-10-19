"""The library query: full-text search, filters and pages (M3.3; AC-M3.6 at service level;
spec 3.3; migration 0003's ``facts_fts``).

``app.services.fact_library.FactLibrary(db).search(query: LibraryQuery) -> LibraryPage``.

``LibraryQuery(text="", role_id=None, skill_id=None, status=None, page=1, page_size=25)``: every
part narrows the result and they combine. ``text`` is what the user typed; it goes through
``app.domain.fts.fts_query`` so each word must be found (in the current version's text or story
parts, the role's employer or title, or a skill name) and FTS5 operators are plain text.
``status=None`` never returns discarded facts.

``LibraryPage``: ``items`` (``FactView`` of the current version, in a stable order), ``total``
(matches on all pages), ``page`` and ``page_size``.

Real migrated SQLite in a temp folder, ``FrozenClock`` and ``SequentialIdFactory``. Application
modules are imported inside the tests so each fails on its own while they are missing. All data
is fictional.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.domain.facts import FactContent, FactStatus, FactType
from app.modules.storage import Database
from tests.fact_bank import START, Bank, add_import, import_drafts, make_bank, open_migrated, typed
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

pytestmark = pytest.mark.integration


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


@dataclass(frozen=True)
class Seeded:
    bank: Bank
    facts: dict[str, Any]  # by short name
    roles: dict[str, str]  # short name -> role id
    skills: dict[str, str]  # skill name -> skill id

    def names(self, page: Any) -> set[str]:
        by_id = {view.id: name for name, view in self.facts.items()}
        return {by_id[item.id] for item in page.items}


@pytest.fixture
def seeded(bank: Bank, db: Database) -> Seeded:
    """Six fictional facts:

    ========== ========= ============ ========================= =======================
    name       status    role         skills                    wording
    ========== ========= ============ ========================= =======================
    close      confirmed lanternfish  Forecasting               month-end close
    backlog    confirmed lanternfish  Scheduling                story: backlog, rota
    ledger     confirmed brightwater  Forecasting, Mentoring    volunteers, ledger
    stocktake  draft     (none)       Scheduling                quarterly stocktake
    newsletter archived  brightwater  (none)                    staff newsletter
    fair       discarded (none)       (none)                    harvest fair
    ========== ========= ============ ========================= =======================
    """
    from app.services.facts import FactInput

    lanternfish = bank.roles.create(employer="Lanternfish Logistics", title="Operations Analyst")
    brightwater = bank.roles.create(employer="Brightwater Cooperative", title="Finance Lead")
    add_import(db, "imp-0001")

    close = typed(
        bank,
        "Cut month-end close from nine days to four",
        role_id=lanternfish.id,
        skills=("Forecasting",),
    )
    backlog = bank.facts.create(
        FactInput(
            type=FactType.STORY,
            situation="The depot's picking backlog had grown badly",
            action="Rebuilt the picking rota around delivery windows",
            result="Everything shipped within a fortnight",
            role_id=lanternfish.id,
            skills=("Scheduling",),
        )
    )
    ledger = typed(
        bank,
        "Trained six volunteers on the booking ledger",
        role_id=brightwater.id,
        skills=("Forecasting", "Mentoring"),
    )
    (stocktake,) = import_drafts(
        bank, "imp-0001", ["Ran the quarterly stocktake"], skills=("Scheduling",)
    )
    newsletter = typed(bank, "Edited the monthly staff newsletter", role_id=brightwater.id)
    newsletter = bank.facts.archive(newsletter.id, expected_version=newsletter.version)
    (fair,) = import_drafts(bank, "imp-0001", ["Organised the harvest fair"])
    fair = bank.facts.discard(fair.id, expected_version=fair.version)

    return Seeded(
        bank=bank,
        facts={
            "close": close,
            "backlog": backlog,
            "ledger": ledger,
            "stocktake": stocktake,
            "newsletter": newsletter,
            "fair": fair,
        },
        roles={"lanternfish": lanternfish.id, "brightwater": brightwater.id},
        skills={skill.name: skill.id for skill in bank.skills.list()},
    )


def _search(seeded: Seeded, **parts: Any) -> Any:
    from app.services.fact_library import LibraryQuery

    return seeded.bank.library.search(LibraryQuery(**parts))


def _found(seeded: Seeded, **parts: Any) -> set[str]:
    return seeded.names(_search(seeded, **parts))


# --- full-text search ------------------------------------------------------------

FULL_TEXT = [
    pytest.param("month-end", {"close"}, id="fact-text"),
    pytest.param("MONTH-END Close", {"close"}, id="any-case"),
    pytest.param("backlog", {"backlog"}, id="story-situation"),
    pytest.param("rota", {"backlog"}, id="story-action"),
    pytest.param("fortnight", {"backlog"}, id="story-result"),
    pytest.param("picking fortnight", {"backlog"}, id="words-in-different-parts"),
    pytest.param("Lanternfish", {"close", "backlog"}, id="role-employer"),
    pytest.param("analyst", {"close", "backlog"}, id="role-title"),
    pytest.param("finance lead", {"ledger"}, id="role-title-two-words"),
    pytest.param("mentoring", {"ledger"}, id="skill-name"),
    pytest.param("forecasting", {"close", "ledger"}, id="skill-on-two-facts"),
    pytest.param("volunteers ledger", {"ledger"}, id="every-word-must-match"),
    pytest.param("volunteers rota", set(), id="words-from-different-facts"),
    pytest.param("ledger brightwater mentoring", {"ledger"}, id="text-role-and-skill-together"),
    pytest.param("zeppelin", set(), id="no-match"),
]


@pytest.mark.parametrize(("text", "expected"), FULL_TEXT)
def test_ac_m3_6_full_text_finds_text_story_parts_role_and_skill(
    seeded: Seeded, text: str, expected: set[str]
) -> None:
    page = _search(seeded, text=text, status=FactStatus.CONFIRMED)

    assert seeded.names(page) == expected
    assert page.total == len(expected)


def test_search_results_carry_the_current_wording_and_skills(seeded: Seeded) -> None:
    page = _search(seeded, text="ledger", status=FactStatus.CONFIRMED)

    (item,) = page.items
    assert item == seeded.bank.facts.get(seeded.facts["ledger"].id)
    assert item.content == FactContent(text="Trained six volunteers on the booking ledger")
    assert item.skills == ("Forecasting", "Mentoring")
    assert item.role_id == seeded.roles["brightwater"]


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_text_is_no_text_filter(seeded: Seeded, blank: str) -> None:
    assert _found(seeded, text=blank, status=FactStatus.CONFIRMED) == {
        "close",
        "backlog",
        "ledger",
    }


def test_search_reaches_drafts_and_archived_facts_but_never_discarded_ones(
    seeded: Seeded,
) -> None:
    assert _found(seeded, text="stocktake", status=FactStatus.DRAFT) == {"stocktake"}
    assert _found(seeded, text="newsletter", status=FactStatus.ARCHIVED) == {"newsletter"}
    # With no status chosen, a discarded draft still isn't in the library.
    assert _found(seeded, text="harvest") == set()
    assert "fair" not in _found(seeded)
    assert {"close", "backlog", "ledger"} <= _found(seeded)


# --- filters ---------------------------------------------------------------------

FILTERS = [
    pytest.param({"status": "confirmed"}, {"close", "backlog", "ledger"}, id="status-confirmed"),
    pytest.param({"status": "draft"}, {"stocktake"}, id="status-draft"),
    pytest.param({"status": "archived"}, {"newsletter"}, id="status-archived"),
    pytest.param(
        {"role": "lanternfish", "status": "confirmed"}, {"close", "backlog"}, id="role-lanternfish"
    ),
    pytest.param({"role": "brightwater", "status": "confirmed"}, {"ledger"}, id="role-brightwater"),
    pytest.param({"role": "brightwater", "status": "archived"}, {"newsletter"}, id="role-archived"),
    pytest.param({"role": "lanternfish", "status": "draft"}, set(), id="role-without-drafts"),
    pytest.param(
        {"skill": "Forecasting", "status": "confirmed"}, {"close", "ledger"}, id="skill-forecasting"
    ),
    pytest.param({"skill": "Scheduling", "status": "confirmed"}, {"backlog"}, id="skill-confirmed"),
    pytest.param({"skill": "Scheduling", "status": "draft"}, {"stocktake"}, id="skill-draft"),
    pytest.param({"skill": "Mentoring", "status": "confirmed"}, {"ledger"}, id="skill-mentoring"),
    pytest.param(
        {"role": "lanternfish", "skill": "Forecasting", "status": "confirmed"},
        {"close"},
        id="role-and-skill",
    ),
    pytest.param(
        {"role": "lanternfish", "skill": "Mentoring", "status": "confirmed"},
        set(),
        id="role-and-skill-no-overlap",
    ),
]


@pytest.mark.parametrize(("chosen", "expected"), FILTERS)
def test_ac_m3_6_filters_role_skill_status(
    seeded: Seeded, chosen: dict[str, str], expected: set[str]
) -> None:
    parts: dict[str, Any] = {"status": FactStatus(chosen["status"])}
    if "role" in chosen:
        parts["role_id"] = seeded.roles[chosen["role"]]
    if "skill" in chosen:
        parts["skill_id"] = seeded.skills[chosen["skill"]]

    page = _search(seeded, **parts)

    assert seeded.names(page) == expected
    assert page.total == len(expected)
    assert all(item.status == FactStatus(chosen["status"]) for item in page.items)


def test_filter_by_an_unknown_role_or_skill_finds_nothing(seeded: Seeded) -> None:
    assert _found(seeded, role_id="nope") == set()
    assert _found(seeded, skill_id="nope") == set()


def test_filters_combine_with_search(seeded: Seeded) -> None:
    lanternfish, brightwater = seeded.roles["lanternfish"], seeded.roles["brightwater"]
    forecasting, scheduling = seeded.skills["Forecasting"], seeded.skills["Scheduling"]
    confirmed = FactStatus.CONFIRMED

    # "Lanternfish" alone finds two facts; each filter narrows it.
    assert _found(seeded, text="Lanternfish", status=confirmed) == {"close", "backlog"}
    assert _found(seeded, text="Lanternfish", skill_id=forecasting, status=confirmed) == {"close"}
    assert _found(seeded, text="Lanternfish", skill_id=scheduling, status=confirmed) == {"backlog"}
    assert _found(seeded, text="Lanternfish", role_id=brightwater, status=confirmed) == set()
    # "forecasting" (a skill name) with a role.
    assert _found(seeded, text="forecasting", role_id=brightwater, status=confirmed) == {"ledger"}
    assert _found(seeded, text="forecasting", role_id=lanternfish, status=confirmed) == {"close"}
    # Text with a status.
    assert _found(seeded, text="stocktake", status=FactStatus.DRAFT) == {"stocktake"}
    assert _found(seeded, text="stocktake", status=confirmed) == set()
    assert _found(seeded, text="newsletter", status=confirmed) == set()
    # All four at once.
    assert _found(
        seeded, text="close", role_id=lanternfish, skill_id=forecasting, status=confirmed
    ) == {"close"}
    assert (
        _found(seeded, text="ledger", role_id=lanternfish, skill_id=forecasting, status=confirmed)
        == set()
    )
    page = _search(seeded, text="Lanternfish", skill_id=forecasting, status=confirmed)
    assert page.total == 1


def test_search_sees_only_current_version(seeded: Seeded) -> None:
    from app.services.facts import FactInput

    close = seeded.facts["close"]
    assert _found(seeded, text="nine", status=FactStatus.CONFIRMED) == {"close"}

    edited = seeded.bank.facts.edit(
        close.id,
        FactInput(
            text="Cut month-end close to four days by automating reconciliations",
            role_id=close.role_id,
            skills=close.skills,
        ),
        expected_version=close.version,
    )

    # Version 1 said "nine"; it is history now and no longer found.
    assert _found(seeded, text="nine", status=FactStatus.CONFIRMED) == set()
    page = _search(seeded, text="reconciliations", status=FactStatus.CONFIRMED)
    assert seeded.names(page) == {"close"}
    assert page.items[0].content_version == 2
    assert page.items[0].content == edited.content
    # What didn't change still matches, once.
    still = _search(seeded, text="month-end", status=FactStatus.CONFIRMED)
    assert [item.id for item in still.items] == [close.id]
    assert still.total == 1


def test_search_follows_a_merge_and_its_draft_disappears(seeded: Seeded) -> None:
    close, stocktake = seeded.facts["close"], seeded.facts["stocktake"]

    seeded.bank.facts.merge(
        stocktake.id,
        close.id,
        FactContent(text="Cut month-end close to four days and ran the stocktake"),
        expected_draft_version=stocktake.version,
        expected_target_version=close.version,
    )

    assert _found(seeded, text="stocktake") == {"close"}
    assert _found(seeded, status=FactStatus.DRAFT) == set()
    # The draft's skill came along.
    assert _found(seeded, skill_id=seeded.skills["Scheduling"], status=FactStatus.CONFIRMED) == {
        "close",
        "backlog",
    }


# --- operators are text ----------------------------------------------------------

# Each query would find "close" if FTS5 read its operator; as plain words it finds nothing,
# because no fact holds the extra word.
OPERATORS = [
    pytest.param("close OR zeppelin", set(), id="or"),
    pytest.param("close NOT zeppelin", set(), id="not"),
    pytest.param("clos*", set(), id="prefix-star"),
    pytest.param("text:close", set(), id="column-filter"),
    pytest.param("role:lanternfish", set(), id="column-filter-on-role"),
    pytest.param("NEAR(close four, 10)", set(), id="near"),
    pytest.param("close AND", set(), id="dangling-and"),
    pytest.param("pause OR resume", {"export"}, id="or-is-a-word-that-can-match"),
    pytest.param("not resume", set(), id="not-is-a-word-that-must-match"),
    pytest.param('"close', {"close"}, id="unbalanced-quote"),
    pytest.param('"month-end close"', {"close"}, id="quoted-words"),
    pytest.param("(close", {"close"}, id="open-bracket"),
    pytest.param("close)", {"close"}, id="close-bracket"),
    pytest.param("^close", {"close"}, id="caret"),
    pytest.param("close'; DROP TABLE facts; --", set(), id="sql-looking-text"),
    pytest.param("close \x00four", {"close"}, id="nul-character"),
    pytest.param("***", set(), id="only-symbols"),
    pytest.param('"""', set(), id="only-quotes"),
]


@pytest.mark.parametrize(("text", "expected"), OPERATORS)
def test_fts_operators_in_query_are_literal(
    seeded: Seeded, db: Database, text: str, expected: set[str]
) -> None:
    export = typed(seeded.bank, "Either pause or resume the nightly export")
    by_id = {view.id: name for name, view in seeded.facts.items()} | {export.id: "export"}

    # Never a syntax error, whatever was typed.
    page = _search(seeded, text=text, status=FactStatus.CONFIRMED)

    assert {by_id[item.id] for item in page.items} == expected
    assert page.total == len(expected)
    assert db.connection.execute("SELECT count(*) FROM facts").fetchone()[0] == 7


# --- pages -----------------------------------------------------------------------


def test_results_are_paged_with_a_total(bank: Bank) -> None:
    from app.services.fact_library import LibraryQuery

    made = [typed(bank, f"Wrote depot procedure number {n} for the handbook") for n in range(1, 8)]
    typed(bank, "Edited the monthly staff newsletter")

    def page(number: int, text: str = "handbook") -> Any:
        return bank.library.search(
            LibraryQuery(text=text, status=FactStatus.CONFIRMED, page=number, page_size=3)
        )

    pages = [page(1), page(2), page(3)]
    assert [len(found.items) for found in pages] == [3, 3, 1]
    assert [found.total for found in pages] == [7, 7, 7]
    assert [(found.page, found.page_size) for found in pages] == [(1, 3), (2, 3), (3, 3)]
    seen = [item.id for found in pages for item in found.items]
    assert len(set(seen)) == 7
    assert set(seen) == {view.id for view in made}
    # The same question gets the same order.
    assert [item.id for item in page(2).items] == [item.id for item in pages[1].items]

    beyond = page(4)
    assert beyond.items == ()
    assert beyond.total == 7

    # Paging works the same without text.
    unfiltered = [page(number, text="") for number in (1, 2, 3)]
    assert [len(found.items) for found in unfiltered] == [3, 3, 2]
    assert {found.total for found in unfiltered} == {8}
    assert len({item.id for found in unfiltered for item in found.items}) == 8


@pytest.mark.parametrize(("page", "page_size"), [(0, 25), (-1, 25), (1, 0), (1, -5)], ids=str)
def test_impossible_page_numbers_refused(bank: Bank, page: int, page_size: int) -> None:
    from app.services.fact_library import LibraryQuery

    with pytest.raises(ValueError, match="page"):
        bank.library.search(LibraryQuery(page=page, page_size=page_size))
