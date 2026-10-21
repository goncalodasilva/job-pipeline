"""Markup and routes of Settings > Board (M5.7; AC-M5.1; ADR-102 item 10, ADR-104), and small
HTML readers for the integration and end-to-end tests. The implementer matches these names.

``GET /settings`` (a page route: no token needed; with ``HX-Request`` and the token the main
content only) gains ``<section id="board-settings" class="settings-section"
aria-labelledby="board-settings-heading">`` with ``<h2 id="board-settings-heading" tabindex="-1">
Board</h2>``. A GET writes nothing. Inside it:

- ``<h3 id="board-statuses-heading" tabindex="-1">Statuses</h3>`` and ``<ul id="status-list">``
  with one ``<li id="status-<key>" data-status-key="<key>" data-phase="preparing|active|closed">``
  per status, in the order of ``ApplicationStatuses.listing()`` (Preparing, then Active, then
  Closed; each by position). The phase is also written in words in the row ("Preparing",
  "Active" or "Closed"), never by colour alone. A row holds three forms (each with a hidden
  ``version``, the status's own):
  * rename: ``action="/settings/board/statuses/<key>/rename"``, field ``label`` (its value is the
    current label; its label text is "Name of <label>"), button "Rename";
  * move: ``action="/settings/board/statuses/<key>/move"``, buttons ``name="direction"
    value="up"`` ("Move up") and ``value="down"`` ("Move down"), whose accessible names end in
    the status label (for example "Move up Applied");
  * delete (custom statuses only; a built-in row has no form whose action ends in ``/delete``):
    ``action="/settings/board/statuses/<key>/delete"``, button "Delete" (accessible name "Delete
    <label>").
  A refused delete shows its message in the row as ``<p id="status-<key>-error" role="alert">``.
- ``<form id="status-add-form" method="post" action="/settings/board/statuses">``: ``label``
  (labelled "New status name") and ``phase`` (a select labelled "Phase" whose first option is
  the empty "Choose a phase", then ``preparing`` "Preparing", ``active`` "Active" and ``closed``
  "Closed"), button "Add status". There is no version: adding changes no existing row.
- ``<h3 id="board-reminders-heading" tabindex="-1">Reminders</h3>`` and ``<ul id="reminder-list">``
  with one ``<li id="reminder-<key>" data-rule-status="<key>">`` for every status that is not
  closed, in the same order. Its form posts to ``/settings/board/reminders/<key>`` with a hidden
  ``version`` (the rule's, ``0`` while the status has no rule), a checkbox ``enabled`` (value
  ``1``; checked when the rule is on; its accessible name starts "Remind me if an application
  stays in <label>") and a number input ``days`` (its accessible name holds "days" and the
  label; shows the rule's days, or 14 when there is no rule), button "Save". The seeded rule
  reads: on, 14 days, version 1, for Applied.

Every route is a POST with the launch token and the origin, a form body whose fields each appear
once (an unknown or repeated field, a missing or non-numeric ``version`` and a bad ``direction``
or ``enabled`` are a plain-text 400 "This request isn't valid."), and answers:

- 200 for htmx: the whole ``#board-settings`` section again (every row carries its fresh
  version), a visible ``<p id="board-settings-done">`` with the message, exactly one
  ``data-autofocus`` element in the answer (the changed row for add, rename, move and a rule;
  ``#board-statuses-heading`` after a delete) and an ``announce`` message in ``HX-Trigger``.
  Without htmx: 303 to ``/settings``.
- 422 for a refused value: the section again (plain: the full page) with the typed values kept
  and each refused field marked ``aria-invalid="true"`` with ``aria-describedby`` naming a
  visible error element (ids unique on the page). Only the fields at fault are marked. Focus
  goes to the first invalid field and the announcement says how many need a change.
- 409 for a stale ``version``: the section with the reload prompt (``VersionConflict.message``
  and a "Reload" link to ``/settings``); nothing changes.
- 404 for an unknown or unsafe status key: plain text, ``StatusNotAllowed.message``, the key is
  never echoed.

Messages (announced and shown in ``#board-settings-done``; ``<label>`` is the status's label):

- add: "Added the status <label>."; rename: "Renamed to <label>."; move: "Moved <label> up." or
  "Moved <label> down."; delete: "Deleted the status <label>.".
- a rule: "Reminder for <label> saved." (on) or "Reminder for <label> turned off." (off).
- refused add: "Status not added. 1 field needs a change." ("N fields need a change." for more);
  refused rename: "Status not renamed. 1 field needs a change."; refused rule: "Reminder for
  <label> not saved. 1 field needs a change.".

Field errors (fixed copy): a blank, taken, too long or multi-line status name keeps the messages
of ``app.domain.applications.create_status``; a missing or unknown phase is "Choose Preparing,
Active or Closed for the status." tied to ``phase``; days are ``DAYS_MESSAGE``. A delete the rules
refuse (built-in: "Built-in statuses cannot be deleted."; in use: "This status is still in use.")
is a 422 with that text in ``#status-<key>-error``.

Everything here is fictional.
"""

from typing import Any

from tests.board_pages import by_id, elements
from tests.integration.test_app_shell import Element

SETTINGS = "/settings"
STATUSES_PATH = "/settings/board/statuses"
REMINDERS_PATH = "/settings/board/reminders"
SECTION_ID = "board-settings"
DONE_ID = "board-settings-done"
STATUS_KEYS = (
    "preparing",
    "applied",
    "screening",
    "interviewing",
    "offer",
    "rejected",
    "withdrawn",
    "accepted",
    "declined",
)
PHASES = {
    "preparing": "preparing",
    "applied": "active",
    "screening": "active",
    "interviewing": "active",
    "offer": "active",
    "rejected": "closed",
    "withdrawn": "closed",
    "accepted": "closed",
    "declined": "closed",
}
OPEN_KEYS = ("preparing", "applied", "screening", "interviewing", "offer")  # have a rule row
INVALID = "This request isn't valid."
BUILT_IN_DELETE = "Built-in statuses cannot be deleted."
IN_USE = "This status is still in use."
PHASE_ERROR = "Choose Preparing, Active or Closed for the status."
DAYS_ERROR = "Enter a whole number of days from 1 to 365."
ARABIC_INDIC_14 = chr(0x661) + chr(0x664)
FULLWIDTH_14 = chr(0xFF11) + chr(0xFF14)


def status_path(key: str, action: str) -> str:
    return f"{STATUSES_PATH}/{key}/{action}"


def rule_path(key: str) -> str:
    return f"{REMINDERS_PATH}/{key}"


def section(html: str) -> Element:
    found = by_id(html, SECTION_ID)
    assert found is not None, "the page has no #board-settings section"
    assert found.tag == "section"
    heading = by_id(html, str(found.attrs.get("aria-labelledby")))
    assert heading is not None and heading.tag == "h2" and heading.text == "Board"
    assert heading.within(found)
    return found


def status_rows(html: str) -> dict[str, Element]:
    box = section(html)
    return {
        str(e.attrs["data-status-key"]): e
        for e in elements(html)
        if e.tag == "li" and "data-status-key" in e.attrs and e.within(box)
    }


def status_order(html: str) -> list[str]:
    return list(status_rows(html))


def rule_rows(html: str) -> dict[str, Element]:
    box = section(html)
    return {
        str(e.attrs["data-rule-status"]): e
        for e in elements(html)
        if e.tag == "li" and "data-rule-status" in e.attrs and e.within(box)
    }


def forms_in(html: str, container: Element) -> list[Element]:
    return [e for e in elements(html) if e.tag == "form" and e.within(container)]


def form_actions_in(html: str, container: Element) -> list[str]:
    return [str(f.attrs.get("action")) for f in forms_in(html, container)]


def has_delete_form(html: str, key: str) -> bool:
    row = status_rows(html)[key]
    return any(action.endswith("/delete") for action in form_actions_in(html, row))


def controls_in(html: str, container: Element) -> dict[str, Element]:
    """The named controls (input, select, textarea, button) inside ``container``, by name; a
    name used twice (the two move buttons) keeps the first."""
    found: dict[str, Element] = {}
    for e in elements(html):
        if e.tag in ("input", "select", "textarea", "button") and e.within(container):
            name = e.attrs.get("name")
            if name is not None:
                found.setdefault(str(name), e)
    return found


def hidden_version(html: str, container: Element) -> str:
    """The ``version`` the hidden fields inside ``container`` carry: one value (a status row has
    three forms and all carry the status's own version)."""
    values = {
        str(e.attrs["value"])
        for e in elements(html)
        if e.tag == "input"
        and e.within(container)
        and e.attrs.get("type") == "hidden"
        and e.attrs.get("name") == "version"
    }
    assert len(values) == 1, f"expected one version in the container, found {sorted(values)}"
    return values.pop()


def row_version(html: str, key: str) -> str:
    """The ``version`` the status's rename form carries (all three forms carry the same)."""
    return hidden_version(html, status_rows(html)[key])


def rule_version(html: str, key: str) -> str:
    return hidden_version(html, rule_rows(html)[key])


def rule_fields(html: str, key: str) -> dict[str, Any]:
    row = rule_rows(html)[key]
    fields = controls_in(html, row)
    return {
        "enabled": "checked" in fields["enabled"].attrs,
        "days": fields["days"].attrs.get("value"),
        "version": rule_version(html, key),
        "action": next(iter(form_actions_in(html, row)), None),
    }


def accessible_name(html: str, control: Element) -> str:
    """aria-label, else the ``label for`` the control's id, else a label around it."""
    label = control.attrs.get("aria-label")
    if label:
        return str(label)
    control_id = control.attrs.get("id")
    for e in elements(html):
        if e.tag == "label" and control_id and e.attrs.get("for") == control_id:
            return e.text
    for ancestor in control.ancestors():
        if ancestor.tag == "label":
            return ancestor.text
    return ""


def errors_in(html: str) -> dict[str, str]:
    """Each control marked ``aria-invalid`` (by name) with the visible text it is described by."""
    found: dict[str, str] = {}
    for e in elements(html):
        if e.attrs.get("aria-invalid") != "true":
            continue
        for target in (e.attrs.get("aria-describedby") or "").split():
            named = by_id(html, target)
            if named is not None and named.text:
                found[str(e.attrs.get("name"))] = named.text
                break
    return found


def invalid_names(html: str) -> list[str]:
    return [
        str(e.attrs.get("name")) for e in elements(html) if e.attrs.get("aria-invalid") == "true"
    ]


def autofocus_count(html: str) -> int:
    box = section(html)
    return len([e for e in elements(html) if "data-autofocus" in e.attrs and e.within(box)])


def done_text(html: str) -> str | None:
    found = by_id(html, DONE_ID)
    return None if found is None else found.text
