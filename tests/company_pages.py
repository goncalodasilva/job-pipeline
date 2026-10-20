"""Fictional company data and small HTML readers for the M4.10 company tests.

Assumed markup (the implementer matches it; the same ids and names are used by the e2e tests):

- ``GET /companies``: ``#companies`` holds ``<table id="companies-table">`` with a header row
  of ``Name``, ``Open roles``, ``To apply`` and ``Last activity`` and one body row per company
  in that cell order. Name cells link to ``/companies/{id}``; the last-activity cell holds a
  ``<time>`` with the date (``YYYY-MM-DD``).
- ``GET /companies/{id}``: ``#company-detail`` with ``<h1>`` (the name), the edit form in
  ``#company-edit`` and the jobs in ``#company-roles``, one ``li`` or ``tr`` per job holding a
  link to ``/jobs/{job id}`` and the state's label.
"""

import re
from html.parser import HTMLParser
from typing import Any

from app.domain.job_normalization import company_key
from app.modules.storage import Database
from app.modules.storage.jobs_repo import insert_company, insert_job

COMPANY_STAMP = "2026-10-01T09:00:00+00:00"

# id, name, jobs as (state, activity date). The names mix cases on purpose: sorting by name is
# not by code point. Every company's jobs share one stamp, so "last activity" is the same
# whether it reads found_at, updated_at or the company's own row.
SORT_SET: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "company-a",
        "Birch Fictional Farms",
        (("removed", "2026-10-10"), ("to_apply", "2026-10-10"), ("new", "2026-10-10")),
    ),
    ("company-b", "alder Fictional Mills", (("new", "2026-10-15"),)),
    (
        "company-c",
        "Cedar Fictional Works",
        (
            ("seen", "2026-10-12"),
            ("seen", "2026-10-12"),
            ("snoozed", "2026-10-12"),
            ("to_apply", "2026-10-12"),
        ),
    ),
    ("company-d", "Dahlia Fictional Co", (("removed", "2026-10-18"), ("removed", "2026-10-18"))),
)
# id -> (open roles, to apply, last activity)
SORT_COUNTS = {
    "company-a": (2, 1, "2026-10-10"),
    "company-b": (1, 0, "2026-10-15"),
    "company-c": (4, 1, "2026-10-12"),
    "company-d": (0, 0, "2026-10-18"),
}


def seed_sort_set(db: Database) -> None:
    with db.transaction() as conn:
        for company_id, name, jobs in SORT_SET:
            insert_company(
                conn, id=company_id, name=name, name_key=company_key(name), now=COMPANY_STAMP
            )
            for n, (state, day) in enumerate(jobs, start=1):
                stamp = f"{day}T09:00:00+00:00"
                job_id = f"{company_id}-job-{n}"
                insert_job(
                    conn,
                    id=job_id,
                    title=f"Fictional {name} role {n}",
                    snapshot_text="Fictional role text.",
                    company_id=company_id,
                    captured_at=stamp,
                    found_at=stamp,
                    now=stamp,
                )
                until = "2026-11-01" if state == "snoozed" else None
                conn.execute(
                    "UPDATE jobs SET triage_state=?, snooze_until=? WHERE id=?",
                    (state, until, job_id),
                )


def seed_many(db: Database, count: int) -> None:
    """Companies with no jobs, named so that name order is the id order."""
    with db.transaction() as conn:
        for i in range(1, count + 1):
            name = f"Fictional Company {i:02d}"
            insert_company(
                conn,
                id=f"company-{i:04d}",
                name=name,
                name_key=company_key(name),
                now=COMPANY_STAMP,
            )


class _Rows(HTMLParser):
    """Rows of the first table with ``table_id``: each cell's text and every link's href."""

    def __init__(self, table_id: str) -> None:
        super().__init__()
        self.table_id = table_id
        self.inside = False
        self.rows: list[dict[str, Any]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "table" and values.get("id") == self.table_id:
            self.inside = True
        elif self.inside and tag == "tr":
            self.rows.append({"cells": [], "hrefs": [], "aria_sort": []})
        elif self.inside and tag in ("td", "th") and self.rows:
            self._cell = []
            if values.get("aria-sort"):
                self.rows[-1]["aria_sort"].append(values["aria-sort"])
        elif self.inside and tag == "a" and self.rows and values.get("href"):
            self.rows[-1]["hrefs"].append(values["href"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self.inside = False
        elif self.inside and tag in ("td", "th") and self._cell is not None and self.rows:
            self.rows[-1]["cells"].append(" ".join("".join(self._cell).split()))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def table_rows(html: str, table_id: str = "companies-table") -> list[dict[str, Any]]:
    parser = _Rows(table_id)
    parser.feed(html)
    return [row for row in parser.rows if row["cells"]]


def listed_ids(html: str) -> list[str]:
    """Company ids of the body rows, in the order shown (the header row has no company link)."""
    ids = []
    for row in table_rows(html)[1:]:
        found = [h for h in row["hrefs"] if re.fullmatch(r"/companies/[A-Za-z0-9_-]+", h)]
        assert len(found) == 1, row
        ids.append(found[0].rsplit("/", 1)[1])
    return ids


class _Items(HTMLParser):
    """``li`` and ``tr`` items inside the element with ``container``'s id."""

    def __init__(self, container: str) -> None:
        super().__init__()
        self.container = container
        self.depth = 0
        self.items: list[dict[str, Any]] = []
        self._open = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if self.depth == 0 and values.get("id") == self.container:
            self.depth = 1
            return
        if self.depth == 0:
            return
        if tag not in {"br", "img", "input", "hr", "meta", "link"}:
            self.depth += 1
        if tag in ("li", "tr"):
            self.items.append({"hrefs": [], "text": []})
            self._open = True
        elif tag == "a" and self._open and values.get("href"):
            self.items[-1]["hrefs"].append(values["href"])

    def handle_endtag(self, tag: str) -> None:
        if self.depth:
            self.depth -= 1
            if tag in ("li", "tr"):
                self._open = False

    def handle_data(self, data: str) -> None:
        if self.depth and self._open:
            self.items[-1]["text"].append(data)


def role_items(html: str, container: str = "company-roles") -> dict[str, str]:
    """Job id to the text of the item that links to ``/jobs/{id}`` inside ``#company-roles``."""
    parser = _Items(container)
    parser.feed(html)
    found: dict[str, str] = {}
    for item in parser.items:
        for href in item["hrefs"]:
            match = re.fullmatch(r"/jobs/([A-Za-z0-9_-]+)", href)
            if match:
                found[match.group(1)] = " ".join("".join(item["text"]).split())
    return found
