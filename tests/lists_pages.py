"""Shared helpers for the M9.2 part D tests of Settings > Lists and statuses: the lists editor
(AC-M9.2; ADR-131 items 6-7; ADR-125 item 5; ADR-042 follow-ups). Every value is fictional. No
application module of part D is imported at module level, so a missing route, template or
module fails the test that needs it, not the collection.

No migration: everything uses ``controlled_values``, ``skills`` and ``jobs`` (migrations 0003 and
0004). Board statuses and reminders (``#board-settings``) are unchanged and stay on the page.

The page (``GET /settings/lists``, a token-free read-only page route; with ``HX-Request`` and the
token the main content only) holds, after ``<section id="board-settings">`` and not inside it,

    <section id="lists-editor" class="settings-section" aria-labelledby="lists-editor-heading">
      <h2 id="lists-editor-heading" tabindex="-1">Lists</h2>
      <a id="values-to-map-link" href="/inbox/values">Values to map</a>   (and some intro text)
      one <details id="list-<key>"> per group, in this order, each collapsed on a page load
         (no ``open``): industry, seniority, employment_type, work_mode, skills
      <section id="removal-reasons" aria-labelledby="removal-reasons-heading">

(Collapsed groups keep the Lists tab budget at 110: one Tab stop per group, ADR-125 item 1.)

Controlled values (industry, seniority, employment_type)

- ``<details id="list-<field>">`` starts with ``<summary id="list-<field>-summary">`` whose text is
  the group name ("Industry", "Seniority", "Employment type"). In the answer to a form of that
  group the details is ``open`` (and the others are not).
- ``<ul id="list-<field>-values">`` holds one ``<li id="value-<id>" data-value-id="<id>"
  data-field="<field>" data-value="<stored value>" tabindex="-1">`` per ``controlled_values``
  row of the field, ordered by value. The row's visible text starts with the label (the value
  with ``_`` as a space) and says in words how many jobs use it ("Used by 3 jobs." or "Not
  used."). The row holds two forms, each with a hidden ``version`` (the row's own):
  * ``<form id="value-<id>-aliases-form" method="post" action="/settings/lists/values/<id>/aliases"
    hx-post="...same...">`` with ``<textarea id="value-<id>-aliases" name="aliases">`` (one other
    spelling per line, labelled "Other spellings of <label>") and a submit button whose
    ``aria-label`` is "Save aliases for <label>" (text "Save aliases");
  * ``<form id="value-<id>-delete-form" ... action="/settings/lists/values/<id>/delete">`` with
    a submit button whose ``aria-label`` is "Delete <label>" (text "Delete"). It is present for
    every value; a value in use is refused when asked (below).
- ``<form id="value-add-form-<field>" method="post" action="/settings/lists/values"
  hx-post="/settings/lists/values">`` with a hidden ``field`` (the field name), a text input
  ``<input id="value-add-<field>" name="value">`` labelled "New <group name lower case> value" and
  a submit button with ``aria-label`` "Add <group name lower case> value" (text "Add value").
  There is no ``version``.

Work mode (read only)

- ``<details id="list-work_mode">`` with summary "Work mode", ``<ul id="list-work_mode-values">``
  of ``<li data-field="work_mode" data-value="remote|hybrid|on_site">``, each showing the label
  and every alias as text, and ``<p id="work-mode-fixed-note">`` saying the list is fixed. Nothing
  inside the details but its summary can be operated: no form, input, select, textarea, button
  or link.

Skills

- ``<details id="list-skills">``, summary "Skills". ``<ul id="skill-list">`` has one ``<li
  id="skill-<id>" data-skill-id="<id>" tabindex="-1">`` per skill (alphabetical as
  ``SkillService.list``), showing the name, with ``<form id="skill-<id>-aliases-form"
  method="post" action="/settings/lists/skills/<id>/aliases" hx-post="...same...">``: hidden
  ``version``, ``<textarea id="skill-<id>-aliases" name="aliases">`` (one alias per line, as
  stored, labelled "Other spellings of <name>") and a button ``aria-label`` "Save aliases for
  <name>". No add and no delete for skills. No skills: ``<p id="skill-list-empty">`` instead of
  the list.

Removal reasons (fixed, read only; ``app.domain.removal_reasons.REMOVAL_REASONS``)

- ``<section id="removal-reasons">`` with ``<h3 id="removal-reasons-heading">Removal
  reasons</h3>`` and ``<ul id="removal-reasons-list">`` of one ``<li id="removal-reason-<key>"
  data-reason="<key>" data-maps-to="<preference fields, space separated, '' for none>">`` per
  reason in CHECK-list order, showing the plain label and, in words, the preference field(s) it
  maps to ("No preference field" for none). Nothing in it can be operated, and no route changes
  the list.

The routes (POST; launch token and origin; the form's fields each exactly once and nothing
else, otherwise a plain-text 400 "This request isn't valid."; ``version`` a whole number from 1)

- ``/settings/lists/values`` adds ``value`` to ``field`` (no version). The stored value is the
  typed text trimmed and lower-cased (``value_key`` of it is kept); a blank value, a value of
  more than ``OVER_LENGTH`` characters and a value whose ``value_key`` equals the key of any
  value or alias of the same field are 422 on the ``value`` input. A field that is not
  industry, seniority or employment_type (``work_mode`` and unknown names) writes nothing and is
  a 400 or a 422.
- ``/settings/lists/values/{id}/aliases`` replaces the alias list (``version``, ``aliases``: one
  per line, CR LF or LF; blank lines dropped; each alias trimmed and lower-cased; exact repeats
  and the value's own text dropped, but spellings that differ only in punctuation or spacing are
  kept, so the seeded lists survive a save; at most 50 lines; no control characters). The
  version is checked first (409); then an alias whose ``value_key`` equals the key of another
  value or alias of the same field, or is over ``OVER_LENGTH`` characters, more than 50 lines or
  one with a control character, is 422 on ``aliases``. The same text may be an alias in another
  field. A new value with a control character is 422 on ``value``.
- ``/settings/lists/values/{id}/delete`` (``version``) deletes only a value no ``jobs`` row holds
  for its field (any triage state, user-corrected or not): stale version -> 409 (checked first);
  in use -> ``IN_USE_STATUSES`` with ``<p id="value-<id>-delete-error" tabindex="-1"
  data-autofocus>`` naming the count of jobs ("3 jobs", "1 job"), the delete button
  ``aria-describedby`` it (first, then the group's "This can't be undone." hint), nothing
  deleted.
- ``/settings/lists/skills/{id}/aliases`` replaces a skill's aliases (``version``, ``aliases``:
  one per line) through ``SkillService.set_aliases``; stale -> 409 first; an alias that means
  another skill (``skill_matches``) -> 422 on ``aliases``.
- An unknown or unsafe id is a plain-text 404 (never echoed). An id of a ``work_mode`` row is
  refused (400, 404 or 422) and nothing changes. There is no delete route for skills.

Answers (as the Board's, ``tests/settings_board_pages.py``): htmx success is 200 with the whole
``#lists-editor`` section (no ``<html>``), ``<p id="lists-editor-done">`` with the message,
exactly one ``data-autofocus`` element inside the section and ``announce`` (the message) in
``HX-Trigger``; without htmx it is 303 to ``/settings/lists``. 422: the section (the whole page
when plain) with the typed text kept (escaped), each refused field ``aria-invalid="true"`` with
``aria-describedby`` naming a visible error element, focus on the first invalid field, the
announcement saying how many fields need a change. 409 (stale): the section (whole page when
plain) with ``<p id="lists-editor-conflict" role="alert" data-autofocus>`` holding
``VersionConflict.message`` and a "Reload" link to ``/settings/lists``; nothing changes. Stored
text is escaped everywhere.
"""

import json
from typing import Any, Final

from tests.board_pages import by_id, elements
from tests.integration.test_app_shell import Element

PATH: Final = "/settings/lists"
VALUES_PATH: Final = "/settings/lists/values"
SECTION_ID: Final = "lists-editor"
HEADING_ID: Final = "lists-editor-heading"
DONE_ID: Final = "lists-editor-done"
CONFLICT_ID: Final = "lists-editor-conflict"
VALUES_TO_MAP: Final = "/inbox/values"
INVALID: Final = "This request isn't valid."
OVER_LENGTH: Final = 200  # a text this long is refused whatever the exact limit is

EDITABLE: Final = {
    "industry": "Industry",
    "seniority": "Seniority",
    "employment_type": "Employment type",
}
GROUPS: Final = ("industry", "seniority", "employment_type", "work_mode", "skills")
GROUP_LABELS: Final = {**EDITABLE, "work_mode": "Work mode", "skills": "Skills"}
WORK_MODES: Final = ("remote", "hybrid", "on_site")
IN_USE_STATUSES: Final = (409, 422)
REFUSED_FIELD_STATUSES: Final = (400, 422)
REFUSED_ID_STATUSES: Final = (400, 404, 422)

# What the page must say about the eight removal reasons, written out here (not read from the
# module under test): key -> (label, preference fields, words for them).
REASONS: Final[dict[str, tuple[str, tuple[str, ...], str]]] = {
    "too_junior": ("Too junior", ("seniority",), "Seniority"),
    "too_senior": ("Too senior", ("seniority",), "Seniority"),
    "location": ("Location", ("location", "work_mode"), "Location and work mode"),
    "salary": ("Salary", ("minimum_salary",), "Minimum salary"),
    "industry": ("Industry", ("industry",), "Industry"),
    "company": ("Company", ("company",), "Company"),
    "not_interested": ("Not interested", (), "No preference field"),
    "other": ("Other", (), "No preference field"),
}
NO_MAPPING_WORDS: Final = "No preference field"

HOSTILE: Final = '"><script id="injected">1</script>'


def label(value: str) -> str:
    """A stored value as shown: ``_`` as a space."""
    return value.replace("_", " ")


def added_message(field: str, value: str) -> str:
    return f"Added {label(value)} to {GROUP_LABELS[field]}."


def aliases_message(name: str) -> str:
    return f"Saved the other spellings of {name}."


def deleted_message(field: str, value: str) -> str:
    return f"Deleted {label(value)} from {GROUP_LABELS[field]}."


def add_refused(count: int = 1) -> str:
    return "Value not added. " + _fields(count)


def aliases_refused(count: int = 1) -> str:
    return "Other spellings not saved. " + _fields(count)


def _fields(count: int) -> str:
    return "1 field needs a change." if count == 1 else f"{count} fields need a change."


def jobs_phrase(count: int) -> str:
    return "1 job" if count == 1 else f"{count} jobs"


def section(markup: str) -> Element:
    found = by_id(markup, SECTION_ID)
    assert found is not None, f"no #{SECTION_ID} in the answer"
    assert found.tag == "section"
    return found


def inside(markup: str, container: Element) -> list[Element]:
    return [e for e in elements(markup) if e.within(container)]


def group(markup: str, key: str) -> Element:
    found = by_id(markup, f"list-{key}")
    assert found is not None, f"no #list-{key}"
    assert found.tag == "details"
    assert found.within(section(markup))
    return found


def group_open(markup: str, key: str) -> bool:
    return "open" in group(markup, key).attrs


def value_rows(markup: str, field: str) -> dict[str, Element]:
    """The ``li`` of each value of ``field``, by stored value."""
    box = group(markup, field)
    return {
        str(e.attrs["data-value"]): e
        for e in inside(markup, box)
        if e.tag == "li" and "data-value" in e.attrs
    }


def row_of(markup: str, field: str, value: str) -> Element:
    rows = value_rows(markup, field)
    assert value in rows, f"no {value!r} in {field}: {sorted(rows)}"
    return rows[value]


def skill_rows(markup: str) -> dict[str, Element]:
    box = group(markup, "skills")
    return {
        str(e.attrs["data-skill-id"]): e
        for e in inside(markup, box)
        if e.tag == "li" and "data-skill-id" in e.attrs
    }


def hidden_version(markup: str, container: Element) -> str:
    values = {
        str(e.attrs.get("value"))
        for e in inside(markup, container)
        if e.tag == "input" and e.attrs.get("type") == "hidden" and e.attrs.get("name") == "version"
    }
    assert len(values) == 1, f"expected one version in the row, found {sorted(values)}"
    return values.pop()


def textarea_of(markup: str, container: Element) -> Element:
    found = [e for e in inside(markup, container) if e.tag == "textarea"]
    assert len(found) == 1, f"expected one textarea, found {len(found)}"
    return found[0]


def alias_lines(markup: str, container: Element) -> list[str]:
    """The alias lines the row's textarea shows (blank lines left out)."""
    raw = "".join(textarea_of(markup, container).parts)
    return [line.strip() for line in raw.replace("\r\n", "\n").split("\n") if line.strip()]


def controls_in(markup: str, container: Element) -> list[Element]:
    """Everything inside ``container`` that can be operated (not its ``<summary>``)."""
    return [
        e
        for e in inside(markup, container)
        if e.tag in ("form", "input", "select", "textarea", "button")
        or (e.tag == "a" and e.attrs.get("href") is not None)
    ]


def focus_targets(markup: str) -> list[Element]:
    return [e for e in inside(markup, section(markup)) if "data-autofocus" in e.attrs]


def invalid_names(markup: str) -> list[str]:
    return [
        str(e.attrs.get("name"))
        for e in inside(markup, section(markup))
        if e.attrs.get("aria-invalid") == "true"
    ]


def invalid_controls(markup: str) -> list[Element]:
    return [e for e in inside(markup, section(markup)) if e.attrs.get("aria-invalid") == "true"]


def error_text(markup: str, field: Element) -> str:
    """The visible text of the one error the refused control names with ``aria-describedby``
    (a ``field-error`` element; a hint it also names, such as "One spelling per line.", is not an
    error)."""
    texts = []
    for identifier in (field.attrs.get("aria-describedby") or "").split():
        found = by_id(markup, identifier)
        if found is not None and found.text and "field-error" in str(found.attrs.get("class")):
            texts.append(found.text)
    assert len(texts) == 1, f"#{field.attrs.get('id')} has no single error tied to it"
    return texts[0]


def done_text(markup: str) -> str | None:
    found = by_id(markup, DONE_ID)
    return None if found is None else found.text


def triggers(headers: Any) -> dict[str, Any]:
    raw = headers.get("HX-Trigger")
    assert raw, "the answer has no HX-Trigger"
    value = json.loads(raw)
    assert isinstance(value, dict)
    return value


def said(headers: Any) -> str:
    return str(triggers(headers).get("announce", ""))


def button_named(markup: str, container: Element, aria_label: str) -> Element:
    found = [
        e
        for e in inside(markup, container)
        if e.tag == "button" and e.attrs.get("aria-label") == aria_label
    ]
    assert len(found) == 1, f"expected one button {aria_label!r}, found {len(found)}"
    return found[0]


def label_text(markup: str, control: Element) -> str:
    control_id = control.attrs.get("id")
    assert control_id, "the control has no id for its label"
    labels = [e for e in elements(markup) if e.tag == "label" and e.attrs.get("for") == control_id]
    assert len(labels) == 1, f"expected one label for #{control_id}"
    return labels[0].text


def ids_are_unique(markup: str) -> None:
    ids = [e.attrs.get("id") for e in elements(markup) if e.attrs.get("id")]
    assert len(ids) == len(set(ids)), "ids stay unique"


def value_path(value_id: str, action: str) -> str:
    return f"{VALUES_PATH}/{value_id}/{action}"


def skill_path(skill_id: str) -> str:
    return f"/settings/lists/skills/{skill_id}/aliases"


def seed_id(field: str, value: str) -> str:
    """The id the migration gives a starting value."""
    return f"cv-{field}-{value}"
