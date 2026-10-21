"""Fictional contacts, interactions and small HTML readers for the M5.6 tests.

Contacts and interactions are seeded with plain SQL against the M5 schema (migration 0005), so a
test of one route does not depend on another route. Everything here is fictional.

The markup as built (the integration and e2e tests share these names):

- Company page (``GET /companies/{id}``, no token needed): ``<section id="company-contacts">`` with
  ``<h2 id="company-contacts-heading" tabindex="-1">Contacts</h2>``. The contacts are
  ``<ul id="company-contact-list">`` of ``<li data-contact-id="<id>">`` in name order (case
  insensitive). One item shows the name, role, email, phone, "how you know them", notes, the
  line ``Last contact: YYYY-MM-DD`` when an interaction exists (the latest one; nothing that
  starts ``Last contact: 2`` otherwise) and a link ``/applications/<id>`` for each linked
  application. An email is a ``mailto:`` link only when it has a valid shape. Each item holds an
  edit form inside ``<details>`` whose summary reads "Edit <name>"
  (``action="/companies/<company id>/contacts/<contact id>"`` with a hidden ``version``). With
  no contacts the section says "No contacts yet". The add form
  (``action="/companies/<company id>/contacts"``, button "Add contact") has the fields
  ``name``, ``role``, ``email``, ``phone``, ``relationship`` and ``notes``, labelled "Contact name",
  "Role", "Email", "Phone", "How you know them" and "Contact notes". A refused field has
  ``aria-invalid="true"`` and an ``aria-describedby`` that points at the visible error text; ids
  are unique on the page.
- Company page History: ``<section id="company-history">`` (``<h2>`` "History") with
  ``<ul id="company-history-list">`` of ``<li data-application-id="<id>">``, newest applied
  first, each with the role as a link to ``/applications/<id>``, ``Applied: YYYY-MM-DD`` (or
  ``Applied: Not yet``) and the status in words: "Closed: rejected" for a closed status, the
  status label (``Interviewing``, ``Preparing``) otherwise. With none it says "No applications
  yet". ``<p id="company-reapply-note">`` ("2 applications here in the last 30 days") appears only
  when two or more applications with an applied date in the last 30 days exist.
- Application drawer and page (``#application-detail``): ``<section id="application-contacts">``
  (``<h3 id="application-contacts-heading">Contacts</h3>``) with the linked contacts as
  ``<li data-contact-id>`` (name, role and an unlink form ``action="/applications/<id>/contacts/
  <contact id>/unlink"`` whose button is named "Unlink <name>"), a link form
  (``action="/applications/<id>/contacts"``, ``<select name="contact_id">`` labelled "Contact to
  link" offering only the company's contacts not yet linked, button "Link contact") and an inline
  add form (``action="/applications/<id>/contacts/new"``, labelled "Contact name", "Role",
  "Email", "Phone", "How you know them" and "Contact notes", button "Add and link contact"). The
  add form sits in a ``<details>`` whose ``<summary>`` holds the heading "Add a contact".
  ``<form id="interaction-form" action="/applications/<id>/interactions">`` (its own section, in a
  ``<details>`` whose ``<summary>`` holds the heading "Log an interaction"; not inside
  ``#application-contacts``) has ``date``
  ("Interaction date"), ``channel`` ("Channel": email, call, video, in_person, message, other),
  ``note`` ("Interaction note") and ``contact_id`` ("Contact (optional)", every contact of the
  company), button "Log interaction". Interactions are ``<li data-timeline-kind="interaction">``
  items of the timeline.
- Save messages (``.save-message`` in the drawer, a plain paragraph on the company page, also
  announced): "Contact
  saved.", "Contact linked.", "Contact unlinked.", "Contact added and linked." and "Interaction
  logged.".
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from tests.board_pages import by_id, elements
from tests.integration.test_app_shell import Element

STAMP = "2026-10-01T09:00:00+00:00"
CHANNELS = ("email", "call", "video", "in_person", "message", "other")


def contact_values(**overrides: str) -> dict[str, str]:
    return {
        "name": "Sam Fictional",
        "role": "Hiring manager",
        "email": "sam@example.test",
        "phone": "+00 555 0100",
        "relationship": "Met at a fictional job fair",
        "notes": "Prefers a short email.",
        **overrides,
    }


def interaction_values(**overrides: str) -> dict[str, str]:
    return {
        "date": "2026-10-18",
        "channel": "call",
        "note": "Talked about the fictional orchard team.",
        "contact_id": "",
        **overrides,
    }


# --- seeding (plain SQL; the schema is migration 0005) ------------------------------------------


def insert_contact(
    db: Any,
    contact_id: str,
    company_id: str,
    name: str,
    *,
    role: str = "",
    email: str = "",
    phone: str = "",
    relationship: str = "",
    notes: str = "",
) -> str:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO contacts (id, company_id, name, role, email, phone, relationship, notes,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (contact_id, company_id, name, role, email, phone, relationship, notes, STAMP, STAMP),
        )
    return contact_id


def link_contact(db: Any, application_id: str, contact_id: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO application_contacts (application_id, contact_id, created_at, updated_at)"
            " VALUES (?,?,?,?)",
            (application_id, contact_id, STAMP, STAMP),
        )


def insert_interaction(
    db: Any,
    interaction_id: str,
    *,
    contact_id: str | None,
    application_id: str | None,
    day: str,
    channel: str = "call",
    note: str = "",
) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO interactions (id, contact_id, application_id, happened_at, channel, note,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                interaction_id,
                contact_id,
                application_id,
                f"{day}T12:00:00+00:00",
                channel,
                note,
                STAMP,
                STAMP,
            ),
        )


def company_id_of(db: Any, name: str) -> str:
    with db.read() as conn:
        return str(conn.execute("SELECT id FROM companies WHERE name=?", (name,)).fetchone()[0])


# --- reading a rendered page --------------------------------------------------------------------


def section_items(html: str, section_id: str, attribute: str) -> dict[str, Element]:
    """The ``li`` items with ``attribute`` inside ``#section_id``, keyed by that attribute."""
    box = by_id(html, section_id)
    if box is None:
        return {}
    return {
        str(e.attrs[attribute]): e
        for e in elements(html)
        if e.tag == "li" and attribute in e.attrs and e.within(box)
    }


def contact_items(html: str, section_id: str = "company-contacts") -> dict[str, Element]:
    return section_items(html, section_id, "data-contact-id")


def history_items(html: str) -> dict[str, Element]:
    return section_items(html, "company-history", "data-application-id")


def hrefs_in(html: str, container: Element) -> list[str]:
    return [
        str(e.attrs["href"])
        for e in elements(html)
        if e.tag == "a" and e.within(container) and e.attrs.get("href") is not None
    ]


def form_actions(html: str) -> list[str]:
    return [
        str(e.attrs["action"]) for e in elements(html) if e.tag == "form" and "action" in e.attrs
    ]


def option_values(html: str, field: str, within: str | None = None) -> list[str]:
    """The ``value`` of every option of the ``select`` named ``field`` (inside ``#within``)."""
    box = by_id(html, within) if within else None
    found: list[str] = []
    for select in elements(html):
        if select.tag != "select" or select.attrs.get("name") != field:
            continue
        if box is not None and not select.within(box):
            continue
        found.extend(
            str(o.attrs.get("value", o.text))
            for o in elements(html)
            if o.tag == "option" and o.within(select)
        )
    return found


def tied_error(html: str, field: str) -> str:
    """The visible error text tied (``aria-invalid`` and ``aria-describedby``) to ``field``."""
    for e in elements(html):
        if e.attrs.get("name") != field or e.attrs.get("aria-invalid") != "true":
            continue
        for target in (e.attrs.get("aria-describedby") or "").split():
            named = by_id(html, target)
            if named is not None and named.text:
                return named.text
    return ""


def mailto_hrefs(html: str) -> list[str]:
    return [
        str(e.attrs["href"])
        for e in elements(html)
        if e.tag == "a" and str(e.attrs.get("href", "")).lower().startswith("mailto:")
    ]


def parsed(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).astimezone(UTC)


def at_clock(r: Any) -> Callable[[str], None]:
    """Set the running app's frozen clock to an ISO instant (``2026-10-05T12:00:00+00:00``)."""

    def set_clock(instant: str) -> None:
        r.run.clock.set(datetime.fromisoformat(instant))

    return set_clock
