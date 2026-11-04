"""Shared helpers for the M9.3 part B tests of Settings > Prompts (AC-M9.2; ADR-055; ADR-131
item 7; spec 2.9 rules 2, 3, 12 and 13, 3.3, 3.6). Every value is fictional. Only application
copy that already exists (``TASK_COPY``, ``DEFAULT_TIERS``, ``INVALID_REQUEST``) is imported, so
a missing route or template fails the test that needs it, not the collection.

The library and editor (part A, ``app.state.prompt_editor``) are not under test here; the tests
use them to read versions and to put override files in place.

The pages

- ``GET`` and ``HEAD /settings/prompts``: the area page "Prompts" (side list entry after "Lists
  and statuses", before "Sessions"; ``tests/settings_areas.py``). A token-free, read-only page.
  Its section is ``<section id="prompts-settings" class="settings-section"
  aria-labelledby="prompts-heading">`` with ``<h2 id="prompts-heading">Prompts</h2>`` and
  ``<ul id="prompt-list">`` of one ``<li id="prompt-<task>" data-task="<task>">`` for every task
  of ``app.domain.tiers.DEFAULT_TIERS``, in that order. A row holds one link ``<a
  id="prompt-link-<task>" href="/settings/prompts/<task>">`` whose text is the task's plain label
  (``TASK_COPY``), the default version as text ("v3", from ``status.default_version``) and, as
  text, one element ``data-badge="edited|default-updated|cant-load"`` per badge: "Edited" (an
  override exists), "Default updated" (the override's ``base_version`` differs from the default
  version) and "Can't load" (``status.error``). Nothing on the page can be operated but its
  links: no form, input, textarea, button or select. It holds no prompt text (default or
  override).
- ``GET`` and ``HEAD /settings/prompts/{task}``: the task page, a token-free shell (a ``PAGE_TAG``
  route; ``task`` must be a key of ``DEFAULT_TIERS``, otherwise a fixed plain-text 404 that never
  echoes the name). Same area chrome as the other Settings pages (side list with Prompts marked
  current: ``aria-current`` of ``page`` or ``true``), ``<h1 id="page-heading" data-autofocus>``
  with the task's label, a ``<title>`` that starts with the label and names "Prompts", "Settings"
  and the app. It holds ``<p id="prompt-default-version">`` ("Default version: v3"), the badges as
  above, ``<p id="prompt-base-version">`` ("Based on v2"; only with an override that has a base
  version) and the placeholder ``<section id="prompt-editor" hx-get="/settings/prompts/<task>/
  editor" hx-trigger="load" hx-swap="outerHTML">`` (a heading, a loading line and a ``<noscript>``
  note). No form, input or textarea, no prompt text: an override is user-written text that may
  hold personal data (spec 2.9 rule 13). With ``HX-Request`` and the token: the main content only.
- ``GET /settings/prompts/{task}/editor``: the protected fragment (token required, read only,
  ``Cache-Control: no-store``, never a page). The root is ``<section id="prompt-editor"
  class="settings-section" aria-labelledby="prompt-editor-heading">`` with ``<h2
  id="prompt-editor-heading">`` and no ``hx-get``. Inside, escaped (no ``|safe``):

  * ``<p id="prompt-status" tabindex="-1">``: "Using the default v3" (no override), "Edited, based
    on v3" (override on the current default), "The default was updated (v3 → v4) since you
    edited this prompt" (override based on another version; the numbers are base then default).
  * ``<p id="prompt-load-error">`` only when ``status.error``: a plain message, never prompt text.
  * ``<p id="prompt-notices">``: says the app always adds the untrusted-text notice and the
    honesty rule (contains "always adds", "untrusted" and "honesty").
  * ``<section id="prompt-default">``: the default text, read only (``#prompt-default-system``
    and ``#prompt-default-user`` hold the two sections as text; no form; a textarea there must be
    ``readonly``).
  * ``<form id="prompt-form" method="post" action="/settings/prompts/<task>" hx-post="...same...">``
    with hidden ``version`` (``status.content_version``), ``<textarea id="prompt-system"
    name="system">`` labelled "System", ``<textarea id="prompt-user" name="user">`` labelled
    "User" (prefilled from the override, else the default) and ``<button id="prompt-save"
    type="submit">Save prompt</button>``.
  * ``<form id="prompt-reset-form" method="post" action="/settings/prompts/<task>/reset"
    hx-post="...same...">`` with the same hidden ``version`` and ``<button id="prompt-reset"
    type="submit">Reset to default</button>``, present only when an override exists (also when
    it does not load).
  * ``<section id="prompt-compare">`` only for an override that loads and whose base version is
    not the default version: ``#prompt-compare-system`` and ``#prompt-compare-user`` each hold
    one element of class ``diff-line`` per line of a server-side line diff of the CURRENT default
    against the override. ``data-kind`` is ``added`` (in the override only; text starts with
    "+"), ``removed`` (in the default only; starts with "-") or ``same``; the text of a line
    follows its marker. The copy says it compares the current default with the user's version.
  * A plain load takes no focus: nothing in the fragment has ``data-autofocus``.

The POSTs (launch token and origin, otherwise 403; each field exactly once and nothing else,
otherwise a plain-text 400 ``INVALID_REQUEST``; 413 over the form cap; 404 for an unknown task)

- ``POST /settings/prompts/{task}``: ``version`` (``"none"`` or a 64-character lower-case hex
  string, otherwise 400), ``system``, ``user``. The version is checked before the text: stale
  -> 409 and nothing written. ``PromptRefused`` -> 422. Success -> 200 for htmx.
- ``POST /settings/prompts/{task}/reset``: ``version`` only. Stale -> 409, nothing removed.
  Success (also when there was no override) -> 200 for htmx; the override is removed.

Answers. htmx: the ``#prompt-editor`` fragment (no ``<html>``, never ``hx-get``) and an ``announce``
in ``HX-Trigger``. Plain (no ``HX-Request``): success is 303 to ``/settings/prompts/<task>``;
409 and 422 answer the whole task page with the editor already in place.

- 200 save: "Prompt saved."; reset: "Prompt reset to the default.". The status line has
  ``data-autofocus`` and is the only such element; the hidden versions are the new content version.
- 409: ``<div id="prompt-conflict" role="alert" data-autofocus>`` holding ``VersionConflict.
  message`` and a link to ``/settings/prompts/<task>``; the only ``data-autofocus``.
- 422: the typed text kept (escaped) in the two textareas; at least one textarea has
  ``aria-invalid="true"``, an ``aria-describedby`` naming an element of class ``field-error`` and
  ``role="alert"`` with a fixed plain message per refusal reason; the first invalid textarea has
  ``data-autofocus`` (and is the only one); the announcement starts with "Prompt not saved."
  and, like every message, never repeats the typed text.
"""

import json
import re
from typing import Any, Final

from app.api.task_tier_forms import task_label
from app.domain.tiers import DEFAULT_TIERS
from tests.board_pages import by_id, elements
from tests.integration.test_app_shell import Element
from tests.lists_pages import hidden_version, inside, triggers

PATH: Final = "/settings/prompts"
TASKS: Final = tuple(DEFAULT_TIERS)
TASK: Final = "summarize_job"  # inputs: posting_context, job_id; untrusted: posting_context
UNTRUSTED_NAME: Final = "posting_context"
INVALID: Final = "This request isn't valid."
SAVED: Final = "Prompt saved."
RESET: Final = "Prompt reset to the default."
NOT_SAVED: Final = "Prompt not saved."
BADGE_WORDS: Final = {
    "edited": "Edited",
    "default-updated": "Default updated",
    "cant-load": "Can't load",
}
NOTICE_WORDS: Final = ("always adds", "untrusted", "honesty")
LEAK: Final = "kestrel-leak-marker-5521"  # planted in an override; a token-free page never holds it
MARKER: Final = "kestrel_marker_0042"  # typed into a refused form; no message repeats it
NO_VERSION: Final = "none"
STALE: Final = "0" * 64  # a well-formed content version no file has


def task_path(task: str) -> str:
    return f"{PATH}/{task}"


def editor_path(task: str) -> str:
    return f"{PATH}/{task}/editor"


def reset_path(task: str) -> str:
    return f"{PATH}/{task}/reset"


def label_of(task: str) -> str:
    return task_label(task)


def using_default(default: int) -> str:
    return f"Using the default v{default}"


def edited_on(base: int) -> str:
    return f"Edited, based on v{base}"


def default_updated(base: int, default: int) -> str:
    return f"The default was updated (v{base} → v{default}) since you edited this prompt"


def raw(element: Element) -> str:
    """The element's text as it is in the markup (whitespace kept), entities decoded."""
    return "".join(element.parts)


def one_line(text: str) -> str:
    return " ".join(text.split())


def same_text(a: str, b: str) -> bool:
    """Equal as text, whatever the line endings or the leading and trailing space."""
    return a.replace("\r\n", "\n").strip() == b.replace("\r\n", "\n").strip()


def by_known_id(markup: str, identifier: str) -> Element:
    found = by_id(markup, identifier)
    assert found is not None, f"no #{identifier} in the answer"
    return found


def editor_section(markup: str) -> Element:
    found = by_known_id(markup, "prompt-editor")
    assert found.tag == "section"
    return found


def textarea_text(markup: str, identifier: str) -> str:
    found = by_known_id(markup, identifier)
    assert found.tag == "textarea", f"#{identifier} is not a textarea"
    return raw(found).replace("\r\n", "\n").strip()


def block_text(markup: str, identifier: str) -> str:
    return raw(by_known_id(markup, identifier)).replace("\r\n", "\n").strip()


def badges_in(markup: str, container: Element | None = None) -> list[str]:
    """The text of every element marked ``data-badge`` (inside ``container`` when given)."""
    found = [e for e in elements(markup) if "data-badge" in e.attrs]
    if container is not None:
        found = [e for e in found if e.within(container)]
    for badge in found:
        kind = badge.attrs.get("data-badge")
        assert kind in BADGE_WORDS, f"unknown badge kind {kind!r}"
        assert badge.text == BADGE_WORDS[str(kind)], (kind, badge.text)
    return [badge.text for badge in found]


def row_of(markup: str, task: str) -> Element:
    found = by_known_id(markup, f"prompt-{task}")
    assert found.tag == "li" and found.attrs.get("data-task") == task
    return found


def list_rows(markup: str) -> list[Element]:
    box = by_known_id(markup, "prompt-list")
    return [e for e in inside(markup, box) if e.tag == "li" and "data-task" in e.attrs]


def operable(markup: str, container: Element) -> list[Element]:
    return [
        e
        for e in inside(markup, container)
        if e.tag in ("form", "input", "select", "textarea", "button")
    ]


def autofocus(markup: str) -> list[Element]:
    return [e for e in elements(markup) if "data-autofocus" in e.attrs]


def form_version(markup: str, form_id: str) -> str:
    return hidden_version(markup, by_known_id(markup, form_id))


def invalid_fields(markup: str) -> list[Element]:
    return [
        e for e in elements(markup) if e.tag == "textarea" and e.attrs.get("aria-invalid") == "true"
    ]


def field_error(markup: str, field: Element) -> str:
    """The visible text of the one alert the refused field names with ``aria-describedby``."""
    texts = []
    for identifier in (field.attrs.get("aria-describedby") or "").split():
        found = by_id(markup, identifier)
        if found is not None and "field-error" in found.classes and found.text:
            assert found.attrs.get("role") == "alert", f"#{identifier} is not an alert"
            texts.append(found.text)
    assert len(texts) == 1, f"#{field.attrs.get('id')} has no single error tied to it"
    return texts[0]


def diff_lines(markup: str, block_id: str) -> list[tuple[str, str]]:
    """(data-kind, text) of each ``diff-line`` in the compare block."""
    box = by_known_id(markup, block_id)
    return [
        (str(e.attrs.get("data-kind")), e.text)
        for e in inside(markup, box)
        if "diff-line" in e.classes
    ]


def said_of(headers: Any) -> str:
    return str(triggers(headers).get("announce", ""))


def has_announce(headers: Any) -> bool:
    raw_value = headers.get("HX-Trigger")
    if not raw_value:
        return False
    value = json.loads(raw_value)
    return isinstance(value, dict) and bool(value.get("announce"))


def front_version(data: bytes) -> int:
    found = re.search(rb"(?m)^version: (\d+)$", data)
    assert found is not None, "the override file has no version line"
    return int(found.group(1))
