"""Fictional board data and small HTML readers for the M5.4 board tests.

Assumed markup (the implementer matches it; the integration and e2e tests share these names):

- ``GET /board`` renders ``<section id="board">`` with the page's only ``<h1 id="board-heading"
  tabindex="-1">Board</h1>``, a view toggle (``<nav aria-label="Board view">`` with the links
  "Board" and "Table"; the current one has ``aria-current="true"``) and
  ``<div id="board-content">``.
  With no applications at all, ``#board-content`` holds one ``.empty-state`` (a ``<p>`` of at
  least four words and a link to ``/inbox``) and nothing else.
- Board view: ``<div id="board-columns">`` holds one ``<section class="board-column"
  data-status="<key>">`` per preparing or active status in position order (heading
  ``<h2>`` = label and count, for example "Applied (2)") and then ``<section id="board-closed"
  class="board-column">``. A column's cards are ``<ul class="board-cards" data-sortable
  data-status="<key>">`` of ``<li class="board-card" data-application-id data-version
  data-status>``; only the card carries ``data-application-id``. A card holds the company in an
  ``<h3 class="board-card-title" tabindex="-1">``, the role, a line for the next event (a
  ``<time datetime>`` and the event type) or the days since the last activity in words
  ("today", "yesterday", "3 days ago"), and a menu: ``<button type="button" aria-haspopup="menu"
  aria-expanded="false">Move to…</button>`` (its accessible name starts with "Move to") and a
  ``<form method="post" action="/applications/<id>/status">`` holding ``<input type="hidden"
  name="version">``, ``<input type="hidden" name="source" value="menu">`` and one
  ``<button type="submit" role="menuitem" name="status" value="<key>">`` per status whose text is
  exactly the status label, inside ``<div role="menu">``.
- Closed: ``<button id="board-closed-toggle" aria-expanded="false"
  aria-controls="board-closed-cards">Closed: 3</button>``; expanded,
  ``<ul id="board-closed-cards" class="board-cards">`` lists the cards,
  each with the line "Closed: rejected" (the label in lower case).
- Table view: ``<table id="board-table">`` with a ``<caption>``, a ``<thead>`` row of header
  cells Company, Role, Status, Applied and Last activity (each holding a sort link, with
  ``aria-sort`` on the sorted ``th``) and one ``<tr data-application-id>`` per application in
  ``<tbody>``; page links carry ``page=N``.
- Follow-up dialogs sit in ``#board-content`` as ``<dialog data-modal aria-labelledby>``.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from html.parser import HTMLParser
from typing import Any

from app.domain.job_normalization import company_key
from app.modules.storage import Database, jobs_repo
from tests.integration.test_app_shell import Element, find_all
from tests.integration.test_app_shell import outline as _outline

START = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)

# Several applications per status, so order, counts and the Closed count are all visible.
# job id, role, company, status key.
BOARD_PLAN: tuple[tuple[str, str, str, str], ...] = (
    ("board-job-1", "Greenhouse Technician", "Larkspur Orchards", "preparing"),
    ("board-job-2", "Irrigation Planner", "Birch Fictional Co", "applied"),
    ("board-job-3", "Seed Buyer", "cedar Fictional Works", "applied"),
    ("board-job-4", "Crop Analyst", "Dune Fictional Ltd", "screening"),
    ("board-job-5", "Pallet Driver", "elm Fictional Yard", "interviewing"),
    ("board-job-6", "Boiler Fitter", "Fern Fictional Labs", "rejected"),
    ("board-job-7", "Frost Watcher", "alder Fictional Mills", "rejected"),
    ("board-job-8", "Pruning Lead", "Birch Fictional Co", "withdrawn"),
)


@cache
def outline(html: str) -> list[Element]:
    """The parsed page, once per html string, so an element found by one helper is the very
    element another helper compares with (``Element.within`` compares by identity)."""
    return _outline(html)


@dataclass
class Seeder:
    """Fictional jobs and their applications, written through the app's own service."""

    db: Database
    applications: Any
    statuses: Any
    tick: Callable[[], None]
    stamp: Callable[[], str]
    _companies: dict[str, str]

    @classmethod
    def make(
        cls,
        db: Database,
        applications: Any,
        statuses: Any,
        tick: Callable[[], None],
        stamp: Callable[[], str],
    ) -> "Seeder":
        return cls(db, applications, statuses, tick, stamp, {})

    def job(self, job_id: str, title: str, company: str | None) -> None:
        now = self.stamp()
        with self.db.transaction() as conn:
            company_id = None
            if company is not None:
                company_id = self._companies.get(company)
                if company_id is None:
                    company_id = f"board-company-{len(self._companies) + 1:04d}"
                    self._companies[company] = company_id
                    jobs_repo.insert_company(
                        conn, id=company_id, name=company, name_key=company_key(company), now=now
                    )
            jobs_repo.insert_job(
                conn,
                id=job_id,
                title=title,
                snapshot_text=f"Fictional posting for {title}.",
                company_id=company_id,
                captured_at=now,
                found_at=now,
                now=now,
            )

    def application(
        self, job_id: str, title: str, company: str | None, status: str = "preparing"
    ) -> Any:
        """A new job, applied for, then moved to ``status``; returns the stored application."""
        self.job(job_id, title, company)
        with self.db.read() as conn:
            job = jobs_repo.get_job(conn, job_id)
        assert job is not None
        application = self.applications.apply_to_job(
            job_id, expected_job_version=job.version
        ).application
        self.tick()
        if status != "preparing":
            application = self.applications.move(
                application.id, status, expected_version=application.version, source="board"
            ).application
            self.tick()
        return application

    def plan(self, plan: tuple[tuple[str, str, str, str], ...] = BOARD_PLAN) -> dict[str, Any]:
        return {
            job_id: self.application(job_id, title, company, status)
            for job_id, title, company, status in plan
        }


def stamp_after(clock: Any) -> Callable[[], str]:
    return lambda: str(clock.now().isoformat())


def ticker(clock: Any) -> Callable[[], None]:
    return lambda: clock.advance(timedelta(minutes=1))


# --- reading a rendered board -------------------------------------------------------------------


def elements(html: str) -> list[Element]:
    return outline(html)


def cards(html: str) -> list[Element]:
    return find_all(outline(html), lambda e: e.tag == "li" and "data-application-id" in e.attrs)


def card_status(card: Element) -> str | None:
    """The status key of the ``ul`` that holds the card."""
    for ancestor in card.ancestors():
        if ancestor.tag == "ul":
            return ancestor.attrs.get("data-status")
    return None


def cards_by_status(html: str) -> dict[str, list[str]]:
    """Application ids in each sortable list, in the order shown."""
    found: dict[str, list[str]] = {}
    for card in cards(html):
        status = card_status(card)
        if status is not None:
            found.setdefault(status, []).append(str(card.attrs["data-application-id"]))
    return found


def column_keys(html: str) -> list[str]:
    return [
        str(e.attrs["data-status"])
        for e in outline(html)
        if e.tag == "section" and "board-column" in e.classes and "data-status" in e.attrs
    ]


def menu_of(html: str, application_id: str) -> dict[str, Any]:
    """The card's menu form: its action, hidden fields and the status buttons."""
    items = outline(html)
    [card] = [
        e for e in items if e.tag == "li" and e.attrs.get("data-application-id") == application_id
    ]
    forms = [e for e in items if e.tag == "form" and e.within(card)]
    assert len(forms) == 1, f"expected one menu form in the card, found {len(forms)}"
    form = forms[0]
    hidden = {
        str(e.attrs["name"]): e.attrs.get("value")
        for e in items
        if e.tag == "input" and e.within(form) and e.attrs.get("type") == "hidden"
    }
    buttons = {
        str(e.attrs["value"]): e.text
        for e in items
        if e.tag == "button" and e.within(form) and e.attrs.get("name") == "status"
    }
    return {
        "action": form.attrs.get("action"),
        "hidden": hidden,
        "buttons": buttons,
        "card": card,
        "version": card.attrs.get("data-version"),
    }


def by_id(html: str, element_id: str) -> Element | None:
    found = [e for e in outline(html) if e.attrs.get("id") == element_id]
    assert len(found) <= 1, f"id {element_id} appears {len(found)} times"
    return found[0] if found else None


def dialogs(html: str) -> list[Element]:
    return [e for e in outline(html) if e.tag == "dialog"]


def dialog_title(html: str) -> str | None:
    """The text of the element that names the (single) dialog."""
    [dialog] = dialogs(html)
    target = dialog.attrs.get("aria-labelledby")
    assert target, "the dialog has no aria-labelledby"
    named = by_id(html, target)
    assert named is not None
    return named.text


def fields_in(html: str, container: Element) -> dict[str, Element]:
    return {
        str(e.attrs["name"]): e
        for e in outline(html)
        if e.tag in ("input", "textarea", "select") and e.within(container) and "name" in e.attrs
    }


class _Table(HTMLParser):
    def __init__(self, table_id: str) -> None:
        super().__init__()
        self.table_id = table_id
        self.inside = False
        self.caption: list[str] = []
        self._in_caption = False
        self.headers: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        self._cell: list[str] | None = None
        self._row: dict[str, Any] | None = None
        self._in_head = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "table" and values.get("id") == self.table_id:
            self.inside = True
        elif not self.inside:
            return
        elif tag == "caption":
            self._in_caption = True
        elif tag == "thead":
            self._in_head = True
        elif tag == "tbody":
            self._in_head = False
        elif tag == "tr":
            self._row = {"id": values.get("data-application-id"), "cells": [], "hrefs": []}
            (self.headers if self._in_head else self.rows).append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            if tag == "th":
                self._row.setdefault("aria_sort", []).append(values.get("aria-sort"))
        elif tag in ("a", "button") and self._row is not None and self._cell is not None:
            self._row["hrefs"].append(values.get("href") or values.get("hx-get") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self.inside = False
        elif tag == "caption":
            self._in_caption = False
        elif tag == "thead":
            self._in_head = False
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row["cells"].append(" ".join("".join(self._cell).split()))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._in_caption and self.inside:
            self.caption.append(data)
        if self._cell is not None:
            self._cell.append(data)


def table(html: str, table_id: str = "board-table") -> _Table:
    parser = _Table(table_id)
    parser.feed(html)
    return parser


def table_ids(html: str) -> list[str]:
    """Application ids of the body rows, in the order shown."""
    return [str(row["id"]) for row in table(html).rows if row["id"]]


def normalise_ids(text: str, ids: dict[str, str]) -> str:
    """Replace each real id with its placeholder so two runs can be compared."""
    for real, placeholder in ids.items():
        text = text.replace(real, placeholder)
    return text


def said_headers(headers: Any) -> str:
    """Every ``HX-Trigger*`` header value, joined (an announcement may travel in any)."""
    return " ".join(
        value for name, value in headers.items() if name.lower().startswith("hx-trigger")
    )


WORDS = re.compile(r"\s+")
