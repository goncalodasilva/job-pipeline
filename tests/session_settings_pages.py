"""Markup, routes and copy of Settings > Sessions (M7.8; AC-M7.6; design "Frequency controls" and
Settings "Sessions"; ADR-117 item 8, ADR-119), with small HTML readers for the integration and
end-to-end tests. The implementer matches these names. Every value is fictional.

``GET /settings/sessions`` (the Sessions area, M9.2; a page route: no token needed; with
``HX-Request`` and the token the main content only) holds
``<section id="sessions-settings" class="settings-section" aria-labelledby="sessions-settings-
heading">`` with ``<h2 id="sessions-settings-heading" tabindex="-1">Sessions</h2>``. A GET writes
nothing. Inside it, one ``<form id="sessions-settings-form" method="post" action="/settings/
sessions" hx-post="/settings/sessions" hx-target="#sessions-settings" hx-swap="outerHTML">``.
The heading and a hint are outside, then ONE native disclosure ``<details id="sessions-
disclosure" class="settings-disclosure">`` (as Your details: a collapsed disclosure is one tab
stop, so the Settings Tab count stays within ``MAX_TABS``) whose first child is ``<summary
id="sessions-summary">Change session settings</summary>``. It has NO ``open`` attribute on a plain
load and HAS ``open`` in every answer to the form (200, 422, 409), so a message, an error or the
reload prompt is visible and focus can move inside it. The form, the done line and the reload
prompt are inside it, so they are in the DOM either way. The form holds:

- hidden ``version``: the stored ``sessions`` setting's version, ``0`` while nothing is stored
  (the page then shows the defaults of ``SessionSettings``: weekly, 3, week, not paused);
- ``select[name=frequency]`` labelled "How often to suggest a session": options ``never``
  "Never", ``weekly`` "Weekly", ``twice_weekly`` "Twice a week", ``after_application`` "After
  each application"; the stored one is ``selected``;
- ``select[name=length]`` labelled "Default session length": ``3`` "3 questions", ``5``
  "5 questions";
- ``select[name=ask_later_default]`` labelled "Default for Ask me later": ``next_session`` "Next
  session", ``week`` "In a week", ``month`` "In a month" (a picked date is not a default);
- ``input[type=checkbox][name=paused][value=1]`` labelled "Pause all suggestions", checked when
  paused; unchecked sends nothing;
- a submit button "Save".

``POST /settings/sessions`` (launch token and origin; POST only, GET with the token is a 405).
A form whose fields each appear once. ``version``, ``frequency``, ``length`` and
``ask_later_default`` are required (a select always sends a value); ``paused`` is optional and,
when present, is exactly ``1``. An unknown or repeated field, a missing required field, a
missing, non-numeric or negative ``version`` and a ``paused`` other than ``1`` are a plain-text
400 "This request isn't valid." and change nothing. Otherwise:

- 200 for htmx: the whole ``#sessions-settings`` section again (every control shows the saved
  value; the hidden ``version`` is the new one), a visible ``<p id="sessions-settings-done">``
  saying "Session settings saved.", exactly one ``data-autofocus`` element in the answer
  (``#sessions-settings-heading``) and ``HX-Trigger`` carrying that same announcement. Without
  htmx: 303 to ``/settings/sessions``. The value is stored through ``SettingsService`` under the key
  ``sessions`` (``SESSIONS``), version ``expected_version`` + 1 (``0`` inserts, version 1).
- 422 for a refused value: the section again (plain: the full Settings page, whose ``<h1>`` has
  no ``data-autofocus``), changes nothing. Each refused field, and only those, has
  ``aria-invalid="true"`` and ``aria-describedby`` naming a visible ``<p class="field-error">``
  with the fixed copy below (ids unique on the page); the first refused field in form order is
  the one ``data-autofocus`` element of the answer. The accepted fields keep what was sent
  (selects selected, checkbox checked); a refused field is never shown back (the select shows the
  saved value). The announcement is "Session settings not saved. 1 field needs a change." ("N
  fields need a change." for more). The hidden ``version`` is the stored one, so sending the
  corrected form from the 422 answer saves.
- 409 for a stale ``version`` (including ``0`` once the setting exists): the section with the
  reload prompt ``<div id="sessions-settings-conflict" role="alert" data-autofocus>`` holding
  ``VersionConflict.message`` and a "Reload" link to ``/settings/sessions`` (the shared
  ``fragments/shell/_reload_prompt.html``); changes nothing; that prompt is the one
  ``data-autofocus`` element of the answer.

Refusals (checked in this order of fields, so several can apply at once): ``frequency`` is not
one of the four values, ``length`` is not exactly the text "3" or "5" (ASCII digits; "03", "4",
"٣" and "" are refused), ``ask_later_default`` is not one of the three values ("date" is refused).

Everything here is fictional.
"""

from typing import Any

from tests.board_pages import by_id, elements
from tests.integration.test_app_shell import Element

SETTINGS = "/settings/sessions"  # M9.2: the Sessions area (was /settings)
SAVE_PATH = "/settings/sessions"
SECTION_ID = "sessions-settings"
FORM_ID = "sessions-settings-form"
DONE_ID = "sessions-settings-done"
CONFLICT_ID = "sessions-settings-conflict"
HEADING_ID = "sessions-settings-heading"
SAVED = "Session settings saved."
NOT_SAVED = "Session settings not saved."
INVALID = "This request isn't valid."

FREQUENCIES = {
    "never": "Never",
    "weekly": "Weekly",
    "twice_weekly": "Twice a week",
    "after_application": "After each application",
}
LENGTHS = {"3": "3 questions", "5": "5 questions"}
ASK_LATER = {"next_session": "Next session", "week": "In a week", "month": "In a month"}
OPTIONS = {"frequency": FREQUENCIES, "length": LENGTHS, "ask_later_default": ASK_LATER}
LABELS = {
    "frequency": "How often to suggest a session",
    "length": "Default session length",
    "ask_later_default": "Default for Ask me later",
    "paused": "Pause all suggestions",
}
FIELD_ERRORS = {
    "frequency": "Choose never, weekly, twice a week or after each application.",
    "length": "Choose 3 or 5 questions.",
    "ask_later_default": "Choose next session, in a week or in a month.",
}
DEFAULTS = {
    "version": "0",
    "frequency": "weekly",
    "length": "3",
    "ask_later_default": "week",
    "paused": False,
}
ARABIC_INDIC_3 = chr(0x663)


def fields_to_change(count: int) -> str:
    return "1 field needs a change." if count == 1 else f"{count} fields need a change."


def section(html: str) -> Element:
    found = by_id(html, SECTION_ID)
    assert found is not None, "the page has no #sessions-settings section"
    assert found.tag == "section" and "settings-section" in (found.attrs.get("class") or "")
    heading = by_id(html, str(found.attrs.get("aria-labelledby")))
    assert heading is not None and heading.tag == "h2" and heading.text == "Sessions"
    assert heading.attrs.get("id") == HEADING_ID and heading.attrs.get("tabindex") == "-1"
    assert heading.within(found)
    return found


def disclosure_open(html: str) -> bool:
    """Whether the section's one native ``<details>`` (with its summary) is open."""
    box = section(html)
    found = [e for e in elements(html) if e.tag == "details" and e.within(box)]
    assert len(found) == 1, "the section holds exactly one disclosure"
    summary = by_id(html, "sessions-summary")
    assert summary is not None and summary.tag == "summary" and summary.within(found[0])
    assert summary.text, "the summary has its own text"
    return "open" in found[0].attrs


def form(html: str) -> Element:
    found = by_id(html, FORM_ID)
    assert found is not None and found.tag == "form" and found.within(section(html))
    assert (found.attrs.get("method") or "").lower() == "post"
    assert found.attrs.get("action") == SAVE_PATH and found.attrs.get("hx-post") == SAVE_PATH
    assert found.attrs.get("hx-target") == f"#{SECTION_ID}"
    return found


def controls(html: str) -> dict[str, Element]:
    box = form(html)
    found: dict[str, Element] = {}
    for e in elements(html):
        if e.tag in ("input", "select", "button") and e.within(box):
            name = e.attrs.get("name")
            if name is not None:
                assert name not in found, f"{name} appears twice in the form"
                found[str(name)] = e
    return found


def options(html: str, select: Element) -> dict[str, str]:
    return {
        str(e.attrs.get("value")): e.text
        for e in elements(html)
        if e.tag == "option" and e.within(select)
    }


def selected(html: str, select: Element) -> str:
    chosen = [
        str(e.attrs.get("value"))
        for e in elements(html)
        if e.tag == "option" and e.within(select) and "selected" in e.attrs
    ]
    assert len(chosen) == 1, f"expected one selected option, found {chosen}"
    return chosen[0]


def shown(html: str) -> dict[str, Any]:
    """What the form shows now: ``version``, the three selected values and ``paused``."""
    found = controls(html)
    assert found["version"].attrs.get("type") == "hidden"
    paused = found["paused"]
    assert paused.attrs.get("type") == "checkbox" and paused.attrs.get("value") == "1"
    return {
        "version": str(found["version"].attrs.get("value")),
        "frequency": selected(html, found["frequency"]),
        "length": selected(html, found["length"]),
        "ask_later_default": selected(html, found["ask_later_default"]),
        "paused": "checked" in paused.attrs,
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


def focus_targets(html: str) -> list[Element]:
    box = section(html)
    return [e for e in elements(html) if "data-autofocus" in e.attrs and e.within(box)]


def done_text(html: str) -> str | None:
    found = by_id(html, DONE_ID)
    return None if found is None else found.text
