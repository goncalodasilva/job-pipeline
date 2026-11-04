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
  aria-labelledby="prompts-heading">`` with ``<h2 id="prompts-heading">Prompts</h2>``, a hint that
  says in plain words that a poorly worded prompt can make results worse or less reliable and that
  Reset goes back to the default, and that the app always adds its own untrusted-text notice and
  honesty rule, and ``<ul id="prompt-list">`` of one ``<li id="prompt-<task>"
  data-task="<task>">`` for every task of ``app.domain.tiers.DEFAULT_TIERS``, in that order. A row
  holds one link ``<a id="prompt-link-<task>" href="/settings/prompts/<task>">`` whose text is
  the task's plain label (``TASK_COPY``), the task's one-line description (``TASK_COPY``), the
  default version as text ("Default v3", from ``status.default_version``) and, as text, one
  element ``data-badge="edited|default-updated|cant-load"`` per badge: "Edited" (an override
  exists), "Default updated" (the override's ``base_version`` is below the default version; a base
  above it is unknown and only "Edited") and "Can't load" (``status.error``). Nothing on the page
  can be operated but its links: no form, input, textarea, button or select. It holds no prompt
  text (default or override).
- ``GET`` and ``HEAD /settings/prompts/{task}``: the task page, a token-free shell (a ``PAGE_TAG``
  route; ``task`` must be a key of ``DEFAULT_TIERS``, otherwise a fixed plain-text 404 that never
  echoes the name). Same area chrome as the other Settings pages (side list with Prompts marked
  current: ``aria-current`` of ``page`` or ``true``), ``<h1 id="page-heading" data-autofocus>``
  with the task's label, a ``<title>`` that starts with the label and names "Prompts", "Settings"
  and the app, an "All prompts" link and the placeholder ``<section id="prompt-editor"
  hx-get="/settings/prompts/<task>/editor" hx-trigger="load" hx-swap="outerHTML">`` (a heading, a
  loading line ``#prompt-editor-loading``, a ``hidden`` failure note ``#prompt-editor-failed`` with
  a "Try again" link to the task page, and a ``<noscript>`` note). There is no summary on the shell
  (no versions, badges or base version): the editor's one status line says where the prompt
  stands. No form, input or textarea, no prompt text: an override is user-written text that may
  hold personal data (spec 2.9 rule 13). With ``HX-Request`` and the token: the main content only.
- ``GET /settings/prompts/{task}/editor``: the protected fragment (token required, read only,
  ``Cache-Control: no-store``, never a page). The root is ``<section id="prompt-editor"
  class="settings-section" aria-labelledby="prompt-editor-heading">`` with ``<h2
  id="prompt-editor-heading">`` and no ``hx-get``. Inside, escaped (no ``|safe``), in this order:

  * ``<p id="prompt-status" tabindex="-1">``: "Using the default v3" (no override), "Edited, based
    on v3" (override on the current default), "The default was updated (v3 → v4) since you
    edited this prompt" (override based on another version; the numbers are base then default).
  * ``<p id="prompt-load-error">`` only when ``status.error``: a plain message, never prompt text.
    It says tasks that use the prompt fail until the text is fixed or reset. When the override is
    a link or a folder the app can't save over (``overridden``, an error and content version
    "none") it says only Reset can fix it, and there is no ``#prompt-form`` at all.
  * ``<p id="prompt-notices">``: says the app always adds the untrusted-text notice and the
    honesty rule (contains "always adds", "untrusted" and "honesty"), that the facts are those and
    the answers the user provides, that the answer format stays the default's and that generated
    text is still checked against the user's facts.
  * ``<h3 id="prompt-version-heading">Your version</h3>``, a plain explanation of System and User
    (``#prompt-sections-hint``), ``<ul id="prompt-inputs">`` with one ``<li>`` per declared input
    of the default (``status.inputs``), each ``<code>`` name followed by the word "untrusted"
    for those in ``status.untrusted_inputs``.
  * ``<form id="prompt-form" method="post" action="/settings/prompts/<task>" hx-post="...same...">``
    with hidden ``version`` (``status.content_version``), ``<textarea id="prompt-system"
    name="system">`` labelled "Your System text", ``<textarea id="prompt-user" name="user">``
    labelled "Your User text" (prefilled from the override, else the default), under each a size
    note ``#prompt-system-size`` / ``#prompt-user-size`` (the current size and the 256 KB limit,
    as text) and ``<button id="prompt-save" type="submit">Save prompt</button>``.
  * only when an override exists (also when it does not load): ``<button id="prompt-reset"
    type="button" data-open-modal="#prompt-reset-dialog">Reset to default</button>`` and, after
    it, ``<dialog id="prompt-reset-dialog" class="modal" data-modal data-modal-manual
    data-return-focus="#prompt-reset">`` titled "Reset this prompt to the default?" with the text
    "Your version will be removed. Unsaved typing here is lost.", the form ``<form
    id="prompt-reset-form" method="post" action="/settings/prompts/<task>/reset"
    hx-post="...same...">`` (hidden ``version``, submit button ``#prompt-reset-confirm``) and the
    safe choice ``<button type="button" data-modal-close autofocus>Keep my version</button>``.
  * ``<section id="prompt-compare">`` only for an override that loads and whose base version is
    below the default version: heading "Your version compared with the current default";
    ``#prompt-compare-system`` and ``#prompt-compare-user`` each hold elements of class
    ``diff-line`` (one per changed line, with two lines of context) and ``diff-gap`` ("… N
    unchanged lines") of a server-side line diff of the CURRENT default against the override. A
    ``diff-line`` has ``data-kind`` ``added`` (in the override only), ``removed`` (in the default
    only) or ``same``, a visually hidden "Added: " / "Removed: " on changed lines, the marker
    ("+", "-" or a space) in ``.diff-marker`` and its text in ``.diff-text``. An unchanged section
    says so instead of listing lines. The copy says it compares the current default with the user's
    version and that it can't show what the update changed.
  * ``<details id="prompt-default">`` (closed) with "Default text (read only)": ``#prompt-default-
    system`` and ``#prompt-default-user`` hold the two sections as text; no form; a textarea there
    must be ``readonly``.
  * a second "All prompts" link ``#prompt-back-link-end`` after the forms.
  * A plain load takes no focus: nothing in the fragment has ``data-autofocus``.

The POSTs (launch token and origin, otherwise 403; each field exactly once and nothing else,
otherwise a plain-text 400 ``INVALID_REQUEST``; 413 over the form cap; 404 for an unknown task)

- ``POST /settings/prompts/{task}``: ``version`` (``"none"`` or a 64-character lower-case hex
  string, otherwise 400), ``system``, ``user``. The version is checked before the text: stale
  -> 409 and nothing written. ``PromptRefused`` -> 422. Success -> 200 for htmx.
- ``POST /settings/prompts/{task}/reset``: ``version`` only. Stale -> 409, nothing removed.
  Success (also when there was no override) -> 200 for htmx; the override is removed.

Answers. htmx: the ``#prompt-editor`` fragment (no ``<html>``, never ``hx-get``, never an
out-of-band summary) and, for a success only, an ``announce`` in ``HX-Trigger``. Plain (no
``HX-Request``): success is 303 to ``/settings/prompts/<task>``; 409 and 422 answer the whole task
page with the editor already in place.

- 200 save: "Prompt saved."; reset: "Prompt reset to the default.". The status line has
  ``data-autofocus`` and is the only such element; the hidden versions are the new content version.
- 409: ``<div id="prompt-conflict" role="alert" data-autofocus>`` holding ``VersionConflict.
  message`` and a link to ``/settings/prompts/<task>``; the only ``data-autofocus``. For a save the
  typed text stays under it in two ``readonly`` textareas ``#prompt-unsaved-system`` /
  ``#prompt-unsaved-user`` (labelled, escaped, no form, no way to overwrite) with the words "Your
  unsaved text is below so you can copy it.".
- 422: the typed text kept (escaped) in the two textareas; both have ``aria-invalid="true"``, an
  ``aria-describedby`` naming the one element of class ``field-error`` with ``role="alert"`` whose
  fixed plain message per refusal reason starts "Prompt not saved. Check both sections."; the first
  text area has ``data-autofocus`` (and is the only one). A refusal is not announced a second time
  (no ``announce``), and like every message never repeats the typed text.
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
CHECK_BOTH: Final = "Check both sections."
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


def diff_rows(markup: str, block_id: str) -> list[tuple[str, str, str, str]]:
    """(kind, marker, hidden word, text) of each ``diff-line`` and ``diff-gap`` in the compare
    block, in order. A gap is ("gap", "", "", its text)."""
    box = by_known_id(markup, block_id)
    found: list[tuple[str, str, str, str]] = []
    for e in inside(markup, box):
        if "diff-gap" in e.classes:
            found.append(("gap", "", "", e.text))
        elif "diff-line" in e.classes:
            parts = [c for c in inside(markup, e) if c.parent is e]
            hidden = next((c.text for c in parts if "visually-hidden" in c.classes), "")
            marker = next((c.text for c in parts if "diff-marker" in c.classes), "")
            text = next((c.text for c in parts if "diff-text" in c.classes), "")
            found.append((str(e.attrs.get("data-kind")), marker, hidden, text))
    return found


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
