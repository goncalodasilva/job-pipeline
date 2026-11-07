"""M9.5 part A: the command palette (Ctrl/Cmd+K) -- the contract the tests assume, the fictional
data they search and the readers of the fragments (ADR-098 item 7, ADR-131 item 10; detailed design
"Cross-cutting UX"; spec 2.9 rules 2, 3 and 12, 2.10; htmx-ui skill). The paths, ids and data
attributes are listed once here so the implementer sees the whole contract in one place. No app
module is imported at the top, so a missing route or template fails the test that needs it.

Routes (both protected GET fragments: the launch token is required, a request without it or with
a wrong one answers 403; neither writes a row, a file, a task or an event; neither is a page)

- ``GET /palette/search?q=<text>`` (``SEARCH``): the results for what was typed.
  - A blank ``q`` (empty or only spaces) answers the actions only: no result group.
  - Otherwise it searches jobs (``jobs_fts``), companies (names), applications (their job's
    title and company) and facts (``facts_fts``), at most 20 results in total (``MAX_RESULTS``).
    Text typed by the user is passed to the index as quoted tokens (M4.6), so FTS syntax such as
    a quote, ``*``, ``NEAR(`` or ``AND`` never raises a 500. The query is never logged: a GET
    puts it in the address, and uvicorn's access records already drop the query string whole
    (``AccessQueryFilter``, ADR-093), so the contract is "use a GET with ``q`` and write no
    log line of your own that holds it".
  - Results are grouped by kind, each kind in one contiguous group.
  - Actions whose label contains the typed words are listed too (``session`` finds "Start
    session").
- ``GET /palette/recent?path=<p>&path=<p>...`` (``RECENT``): the empty-query state. The client
  sends up to 8 page paths from ``localStorage`` (newest first) and the server resolves their
  titles. Only these page shapes are accepted: ``/jobs/<id>``, ``/companies/<id>``,
  ``/applications/<id>`` and ``/facts/<id>/edit`` where ``<id>`` passes ``is_safe_id`` and
  exists. Anything else (a foreign URL, a protocol-relative URL, a traversal, an unsafe or an
  unknown id, any other page) is dropped without a trace in the answer; at most 8 are shown, in
  the order sent. The answer holds the actions group and, when at least one path is valid, the
  recent group.

Markup (fragment: no ``<html>``, no launch token in it)

- One group element per kind: ``data-palette-group="job|company|application|fact|recent|
  actions"`` holding a heading (``h2``..``h6``) whose text is ``GROUP_HEADINGS[kind]`` and the
  group's items.
- A result: ``<a data-palette-result data-kind="job|company|application|fact" href="...">`` whose
  text is its title (escaped, never ``|safe``). The href is the page of the item
  (``/jobs/<id>``, ``/companies/<id>``, ``/applications/<id>``, ``/facts/<id>/edit``).
- A recent item: ``<a data-palette-recent data-kind="..." href="...">`` with the resolved title.
- An action: ``<a data-palette-action data-action="add-job|new-fact|start-session|new-event"
  href="...">`` with the labels in ``ACTIONS``. The href is a same-origin path (one slash first);
  new fact is ``/facts/new`` and start session is ``/sessions/new``.
- A search answer holds ``<p data-palette-status>`` with the number of results in
  its text; a search with no match holds no result and an element ``data-palette-empty`` with
  text (the empty state).

The dialog (every page that extends ``base.html``, so Today, Inbox, Board, Facts, Companies,
Settings and the rest; ``tests/e2e/test_palette.py``)

- ``<dialog id="palette" data-palette role="dialog" aria-modal="true" aria-labelledby=...>``
  named "Command palette"; opened with ``showModal()`` (so it matches ``:modal`` and the page
  behind is inert) by Ctrl+K on Windows/Linux and Cmd+K on macOS, from anywhere, also while
  typing in a field and also with the single-key shortcuts switched off.
- Inside: ``<input id="palette-input" type="search" role="combobox"...>`` with the accessible
  name "Search jobs, companies, applications and facts", focused when the dialog opens, and
  ``<div id="palette-results">`` that receives the fragments (the empty-query fragment
  ``RECENT`` on open, ``SEARCH`` after typing, without a request per key press beyond a short
  debounce) and a "Close" button. ArrowDown/ArrowUp move the active item; the active item is
  the one with ``aria-selected="true"``; Enter opens it. Esc closes the dialog and focus returns
  to the element that had it before. Tab and Shift+Tab never leave the dialog.
- ``localStorage`` remembers the last 8 item pages visited (kinds above), newest first, so the
  open dialog can ask ``RECENT``. The key name is the implementer's.

Every value below is fictional.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from app.modules.storage import Database
from tests.board_pages import Seeder, elements
from tests.integration.test_app_shell import Element

SEARCH: Final = "/palette/search"
RECENT: Final = "/palette/recent"
MAX_RESULTS: Final = 20
MAX_RECENT: Final = 8
KINDS: Final = ("job", "company", "application", "fact")
GROUP_HEADINGS: Final = {
    "job": "Jobs",
    "company": "Companies",
    "application": "Applications",
    "fact": "Facts",
    "recent": "Recent",
    "actions": "Actions",
}
ACTIONS: Final = {
    "add-job": "Add job",
    "new-fact": "New fact",
    "start-session": "Start session",
    "new-event": "New event",
}
ACTION_HREFS: Final = {"new-fact": "/facts/new", "start-session": "/sessions/new"}
DIALOG: Final = "#palette"
INPUT: Final = "#palette-input"
RESULTS: Final = "#palette-results"

WORD: Final = "quokka"
FACT_TEXT: Final = "Trained a quokka sanctuary volunteer team of 6 people."
OWN_COMPANY: Final = "Lantern Quay Fictional"
OTHER_COMPANY: Final = "Harbor Kite Fictional"
HOSTILE_TITLE: Final = '<script>alert("palette")</script> Heron Rigger'
HOSTILE_COMPANY: Final = "<img src=x onerror=alert(1)> Heron Ltd"
HOSTILE_FACT: Final = "<svg onload=alert(1)> Heron fact with a team of 4."
SEED_START: Final = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Seeded:
    """The ids of the fictional rows ``seed_palette`` wrote."""

    wrangler_job: str
    rigger_job: str
    application: str
    own_company: str
    other_company: str
    fact: str


def company_id(db: Database, name: str) -> str:
    with db.read() as conn:
        row = conn.execute("SELECT id FROM companies WHERE name=?", (name,)).fetchone()
    assert row is not None, name
    return str(row[0])


def make_seeder(
    db: Database, applications: Any, statuses: Any, *, step: Callable[[], None] | None = None
) -> Seeder:
    """A ``Seeder`` with its own frozen stamps (each call to ``tick`` moves a minute on)."""
    now = [SEED_START]

    def tick() -> None:
        now[0] += timedelta(minutes=1)
        if step is not None:
            step()

    return Seeder.make(db, applications, statuses, tick, lambda: now[0].isoformat())


def seed_palette(db: Database, applications: Any, statuses: Any, facts: Any) -> Seeded:
    """Two jobs whose titles hold ``WORD`` (one of them applied for), their two companies and one
    confirmed fact that holds it too. Through the app's own services."""
    from app.services.facts import FactInput

    seeder = make_seeder(db, applications, statuses)
    seeder.job("job-pal-0001", "Quokka Wrangler", OTHER_COMPANY)
    application = seeder.application("job-pal-0002", "Quokka Rigger", OWN_COMPANY, "applied")
    fact = facts.create(FactInput(text=FACT_TEXT))
    return Seeded(
        wrangler_job="job-pal-0001",
        rigger_job="job-pal-0002",
        application=str(application.id),
        own_company=company_id(db, OWN_COMPANY),
        other_company=company_id(db, OTHER_COMPANY),
        fact=str(fact.id),
    )


def seed_hostile(db: Database, applications: Any, statuses: Any, facts: Any) -> Seeded:
    """The same shape with markup in a job title, a company name and a fact text, all holding
    the word ``heron``."""
    from app.services.facts import FactInput

    seeder = make_seeder(db, applications, statuses)
    seeder.job("job-hostile-1", HOSTILE_TITLE, HOSTILE_COMPANY)
    application = seeder.application("job-hostile-2", HOSTILE_TITLE, HOSTILE_COMPANY, "applied")
    fact = facts.create(FactInput(text=HOSTILE_FACT))
    return Seeded(
        wrangler_job="job-hostile-1",
        rigger_job="job-hostile-2",
        application=str(application.id),
        own_company=company_id(db, HOSTILE_COMPANY),
        other_company=company_id(db, HOSTILE_COMPANY),
        fact=str(fact.id),
    )


def item_path(kind: str, item_id: str) -> str:
    return {
        "job": f"/jobs/{item_id}",
        "company": f"/companies/{item_id}",
        "application": f"/applications/{item_id}",
        "fact": f"/facts/{item_id}/edit",
    }[kind]


# --- reading a fragment -------------------------------------------------------------------------


def results(markup: str) -> list[Element]:
    return [e for e in elements(markup) if "data-palette-result" in e.attrs]


def recents(markup: str) -> list[Element]:
    return [e for e in elements(markup) if "data-palette-recent" in e.attrs]


def actions(markup: str) -> list[Element]:
    return [e for e in elements(markup) if "data-palette-action" in e.attrs]


def groups(markup: str) -> list[Element]:
    return [e for e in elements(markup) if "data-palette-group" in e.attrs]


def result_pairs(markup: str) -> list[tuple[str, str]]:
    """(kind, href) of every result, in document order."""
    return [(str(e.attrs.get("data-kind")), str(e.attrs.get("href"))) for e in results(markup)]


def kinds_in_order(markup: str) -> list[str]:
    return [str(e.attrs.get("data-kind")) for e in results(markup)]


def group_heading(group: Element, markup: str) -> str | None:
    for element in elements(markup):
        if element.tag in ("h2", "h3", "h4", "h5", "h6") and element.within(group):
            return element.text
    return None


def row_counts(db: Database) -> dict[str, int]:
    """The row count of every table (the search indexes and their shadow tables included)."""
    with db.read() as conn:
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        return {
            name: int(conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])  # noqa: S608
            for name in names
        }
