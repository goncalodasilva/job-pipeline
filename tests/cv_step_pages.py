"""The running app, assumed routes, markup hooks and readers for the M6.8 CV step tests (step 3 of
the Tailoring tab: diff, inline edits, flags and approval; AC-M6.1, AC-M6.3, AC-M6.4 in the UI;
detailed design "Diff conventions", "Editing rules", "Application workspace" step 3; ADR-106
items 5, 8, 9 and 11; ADR-113 items 5, 8 and 10). Every value is fictional. No application module
of M6.8 is imported here, so a missing route or template fails the test that needs it.

ASSUMED INTERFACE (the implementer follows it; ``DocumentService`` and ``DocumentView`` of M6.7
are used as they are, see ``tests/document_pages.py``).

Routes. ``{id}`` is the application id, ``{uid}`` a unit id, ``{vid}`` a document version id; all
safe ids (``is_safe_id``; anything else, and any unknown application, answers the plain-text
404 ``ApplicationNotFound.message`` that echoes nothing). None of them is a page except the first.
Every one needs the launch token, every POST also the origin. POSTs are URL-encoded; an unknown or
repeated field, or a missing or malformed ``version``, is the plain-text 400 "This request isn't
valid."; a ``version`` that is not the document's current version is a 409 (below).
- ``GET /applications/{id}?tab=tailoring&step=3[&version={vid}]`` (token-free, read only): the
  page. ``version`` shows that older version read-only; an unknown or unsafe ``version`` is
  ignored (the current version is shown, the value is never echoed). Rule 13: the page holds no
  personal detail; its ``#cv-contact`` is a lazy placeholder ``<div id="cv-contact"
  hx-get="/applications/{id}/tailoring/3/contact" hx-trigger="load">`` without a name or detail.
- ``GET /applications/{id}/tailoring/3`` (existing): the ``#tailoring`` fragment with the filled
  ``#cv-contact``. ``GET .../3/version/{vid}``: the same for that version, read-only (404 with the
  plain text "That version wasn't found." when it is not a version of this application).
  ``GET .../3/contact``: ``#cv-contact`` alone. All reads write and start nothing.
- ``POST .../3/generate``: ``note`` (optional, at most 500 characters), ``replace_all`` ("1" when
  ticked), ``confirm_replace`` ("1" when ticked) and the budget dialog's fields (``tier``,
  confirmation). Through the budget gate exactly as M6.5's starts.
- ``POST .../3/unit/{uid}/edit``: ``version``, ``text`` (citations are never posted: a
  ``fact_ids`` field is a 400). ``POST .../3/unit/add``: ``version``, ``section``, ``text``,
  optional ``role`` (a role label). ``POST .../3/unit/{uid}/remove``: ``version``.
  ``POST .../3/unit/{uid}/confirm``: ``version`` ("This is true: save it as a fact" and the yes of
  "Save this as a fact?"). ``POST .../3/approve``: ``version``.
- ``version`` is the DOCUMENT VERSION id the form was drawn from (hidden input ``version`` of every
  form, equal to ``#cv-step[data-version-id]``).

Answers (``#tailoring`` fragment, ``hx-target="#tailoring" hx-swap="outerHTML"``)
- 200 on success, with ``HX-Trigger {"announce": "<text>"}``: generate "CV generation started."
  (an identical running request is the same 200); edit "Line saved."; add "Line added."; remove
  "Line removed."; confirm "Saved as a fact. The line now cites it."; approve "CV approved.". An
  edit/add/remove/confirm made on an APPROVED version says "Saved as a new draft. The approved
  version is unchanged." instead. An edit that makes a new claim says "Line saved. Save this as a
  fact?" and carries the dialog below. A step that refreshes itself after its task succeeded (the
  request header ``HX-Trigger: tailoring``) says "CV ready to review." once.
- Focus: exactly ONE ``data-autofocus`` element in an answer with a message: the edited or added
  line (``li[data-line]``, ``tabindex="-1"``) after edit/add/confirm; ``#cv-heading`` after
  generate/remove/approve; the first invalid control after a 422; the reload prompt after a 409;
  none when the save-as-fact dialog is shown (its "Not now" has ``autofocus`` instead).
- 422 (UnitInvalid, a bad note, ReplaceNotConfirmed): the step again, the typed text kept in its
  control, the control ``aria-invalid="true"`` with ``aria-describedby`` naming a visible error
  element holding the service's own ``message``; announced as that message.
- 409 (GenerateInProgress, ApprovalBlocked, NothingToApprove, GenerateNotReady): the step again
  with ``p#cv-error[role=alert]`` holding the service's own ``message``; announced; nothing
  changed or queued.
- 409 VersionConflict: the step again, showing the CURRENT version, with ``#cv-conflict`` = the
  shared reload prompt (``fragments/shell/_reload_prompt.html``: ``role="alert"``, the text of
  ``VersionConflict.message``, a "Reload the page" link to the step-3 page address) and, when the
  failed post had ``text``, ``textarea#cv-kept-text[readonly]`` labelled "Your unsaved text"
  holding it. Nothing is written.
- 404 plain text for an unknown unit of the current version (``UnitNotFound.message``).

Markup of step 3 inside ``#tailoring[data-step="3"]``, in this DOM order: the country form, then
``section#cv-step``, then the M6.6 ``#cv-template`` unit (so the CV's controls come before the
template's many fields). On step 3 only, ``#tailoring`` has ``data-keep-edits``, ``hx-get`` of its
own fragment and ``hx-trigger`` containing ``tasks:updated from:body`` always, plus ``every 2s``
ONLY while a ``tailor_cv`` task is queued or running; it holds ``p#cv-stale-note[data-stale-note]
[hidden]`` with the words "A newer version of the CV is ready" (shown and announced by app.js when
a refresh was dropped because an editor is open). ``static/js/cv-step.js`` keeps unsaved textarea
text in page memory (never storage) keyed by ``textarea[data-draft-key]`` and puts it back after
a swap with ``#cv-draft-note`` ("Your unsaved typing was put back.", announced).
- Stepper state words for step 3: "Not started" (no version), "Running", "Needs attention" (the
  last ``tailor_cv`` task failed), "Needs your check" (current draft with blockers), "Ready to
  approve" (current draft, no blocker), "Done" (current version approved). No "Coming later" and
  no "later step" text on step 3 any more.
- ``#cv-step`` carries ``data-state`` (``none|running|failed|draft|approved``),
  ``data-version-id`` and ``data-version-number`` (absent without a version) and
  ``data-readonly`` ("true" while viewing an older version or while a task runs, else "false").
  ``h5#cv-heading`` (``tabindex="-1"``) "Tailored CV".
- Not ready (GenerateNotReady): ``p#cv-not-ready`` with the exception's ``message`` verbatim and
  ``a#cv-not-ready-link``: summary/requirements "Open job" (``/jobs/{job id}``), facts "Open
  facts" (``/facts``), country "Choose the CV country" (``#cv-country``), template "Draft the
  template" (``#cv-template-heading``). No ``#cv-generate-form``, no edit control.
- ``form#cv-generate-form`` (when ready, not running, not viewing an old version): ``textarea
  #cv-note`` (``name=note``, label "Note for the CV (optional)"), when a version exists
  ``input#cv-replace-all[type=checkbox][name=replace_all][value=1]`` labelled "Replace everything,
  including my edits" and ``input#cv-confirm-replace[type=checkbox][name=confirm_replace]
  [value=1]`` labelled "Yes, replace my edits too"; submit ``button#cv-generate-start`` "Generate
  CV" (no version) or "Regenerate". A blocked budget makes the button ``aria-disabled="true"``
  with ``aria-describedby`` naming the visible reason (M6.5 pattern); a warning answers with the
  budget dialog ``#tailoring-dialog-3`` whose hidden fields include ``note``, ``replace_all`` and
  ``confirm_replace`` as typed, so "Continue" starts the same request.
- Running: ``p#cv-running[role=status][hx-preserve]`` "Making your CV…"; no generate form, no edit
  control; the version shown so far is read-only. A failed task: ``p#cv-error[role=alert]`` with
  ``message_for_kind(task.error)``. ``p#cv-practice.badge`` holds ``view.practice_notice``.
- ``p#cv-status``: draft "Draft, version N. Not approved yet."; approved "Approved, version N.";
  an older version "Version N (read-only)." ``p#cv-readonly-note`` (older version only) says the
  version is read-only and not the latest, with ``a#cv-latest`` "Back to the latest version".
- ``div#cv-contact`` (fragment): ``p[data-contact-name]``, ``li[data-contact-line]`` per line,
  ``a[href="/settings/your-details"]`` "Change in Settings"; no form, input or button;
  ``p#cv-contact-empty`` when there is no name. Code-built, never editable.
- ``ul#cv-diff-legend`` explains the four marks. Per section ``section[data-section="<id>"]``
  with ``h6[data-section-heading]`` (the snapshot's heading), per role block
  ``div[data-role-block]`` with a header ``[data-role-header]`` (title, employer, and the start
  and end texts of ``RoleHeader``; no control inside it), then ``ul > li[data-line]`` per unit.
- ``li[data-line][data-unit-id][data-mark][data-origin]`` (``id="line-<uid>"``, ``tabindex="-1"``;
  ``data-mark`` is ``added|reworded|moved|unchanged`` from ``view.diff`` by unit id; also
  ``data-moved="true"`` when a reworded line moved; ``data-flagged="true"`` when it has a flag
  or a blocker; ``data-origin`` ``ai|user``):
  - ``[data-diff-mark]``: text "+ Added", "~ Reworded", "↕ Moved" or "= Unchanged" (symbol then
    the word; the symbol is ``aria-hidden`` but the word is always text). A reworded line that
    moved also has ``[data-moved-mark]`` "↕ Moved".
  - ``p[data-line-text]#line-text-<uid>``: the text, escaped. A reworded line shows its word diff
    with ``<del>`` (removed words) and ``<ins>`` (added words), one element per segment of
    ``app.domain.diff.word_diff(master text, new text)``.
  - ``[data-origin-label]``: "Edited by you" for ``data-origin="user"``.
  - ``ul[data-flags] > li[data-flag="unsupported|needs_check"]`` for every stored flag AND every
    ``view.blockers`` entry of that unit (deduplicated): ``[data-flag-label]`` is "This isn't in
    your fact bank." for unsupported, "Check this number" for a needs_check whose claim is a
    number or amount, else "Check this claim"; ``[data-flag-claim]`` is the claim text, escaped.
  - ``details[data-line-facts] > summary`` "Facts used (N)" and ``ul[data-fact-list] > li > a``
    per cited fact: ``href="/facts/<fact id>/edit"`` (only for a safe id), text = the user's own
    wording of the cited version. The list is visible when the line is hovered AND when the
    summary is opened by keyboard; a line citing nothing has ``[data-no-facts]`` "No facts cited."
  - actions (absent while ``data-readonly="true"``): ``details[data-line-edit] > summary`` "Edit"
    holding ``form[data-line-form=edit]`` (hidden ``version``; ``textarea[name=text]`` labelled
    "Edit this line", ``data-draft-key="edit:<uid>"``; submit "Save line"); ``form
    [data-line-form=remove]`` with a button "Remove"; on a flagged line only, ``form
    [data-line-form=confirm]`` with a button "This is true — save it as a fact". Each button has
    ``aria-describedby="line-text-<uid>"``. A 422 for the line opens its ``details`` and ties
    the error to the textarea.
- ``section#cv-removed`` (only when the diff has REMOVED lines) lists the master lines the CV
  leaves out: ``li[data-line][data-mark="removed"]`` with ``[data-diff-mark]`` "U+2212 Removed"
  (the true minus sign) and the master text; no form, button or unit id.
- ``details#cv-add > summary`` "Add a line" (absent while read-only) with ``form#cv-add-form``:
  hidden ``version``, ``select[name=section]`` labelled "Section" (the template snapshot's sections,
  value = id, text = heading), ``select[name=role]`` labelled "Role (optional)" ("No role" = empty
  value, then the role labels shown in the CV), ``textarea[name=text]`` labelled "New line"
  (``data-draft-key="add"``), submit "Add line".
- Approval: ``button#cv-approve`` "Approve CV" (draft, not read-only). When anything blocks it is
  ``aria-disabled="true"`` with ``aria-describedby="cv-approve-reason"`` and ``p#cv-approve-reason``
  says "1 line needs your check." / "N lines need your check." (N = distinct lines in
  ``view.blockers``) or, for a version with no visible line, "There is nothing to approve yet.".
  When nothing blocks the button has no ``aria-disabled`` and there is no ``#cv-approve-reason``.
  An approved current version has no button; ``p#cv-approved`` says "Approved. Changing a line
  starts a new draft."
- ``ol#cv-versions`` (newest first): ``li[data-version-id][data-version-status="draft|approved"]``
  per version: "Version N", the status word "Draft"/"Approved", "Latest" on the current one,
  "Written by the AI" or "Edited by you", the version's note (escaped) when it has one, and
  ``a[href="/applications/{id}?tab=tailoring&step=3&version={vid}"]`` "View version N" (htmx:
  ``hx-get`` of ``.../3/version/{vid}``, target ``#tailoring``, ``hx-push-url`` the href) with
  ``aria-current="true"`` on the one being viewed.
- Save-as-fact question (an edit that makes a new claim): ``dialog#cv-save-fact[data-modal]``
  (``role="dialog" aria-modal="true"``, ``aria-labelledby="cv-save-fact-title"``,
  ``aria-describedby="cv-save-fact-claims"``), ``h5#cv-save-fact-title`` "Save this as a fact?",
  ``ul#cv-save-fact-claims`` with an ``li`` per claim of ``FactOffer.claims``, ``form`` posting to
  ``.../3/unit/{uid of the NEW version's line}/confirm`` with the NEW version id, a submit "Yes,
  save it as a fact", and ``button`` "Not now" with ``autofocus`` and ``data-return-focus
  ="#line-<uid>"``. Declining changes nothing (the line stays flagged).
"""

import html
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from httpx import Response

from tests.board_pages import elements
from tests.document_pages import Documents
from tests.integration.test_app_shell import Element
from tests.refine_harness import refining
from tests.tailoring_pages import by_id, forms_in, has_id, hidden_value, inside, said_text

NOT_IN_FACT_BANK = "This isn't in your fact bank."
CHECK_NUMBER = "Check this number"
CONFIRM_BUTTON = "This is true — save it as a fact"
SAVE_AS_FACT = "Save this as a fact?"
PRACTICE = "Practice data"
REPLACE_ALL = "Replace everything, including my edits"
STALE_NOTE = "A newer version of the CV is ready"
INVALID_REQUEST = "This request isn't valid."
VERSION_NOT_FOUND = "That version wasn't found."
HOSTILE = (
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "</textarea><b>bold</b> & more",
    "<svg onload=alert(2)>claim</svg>",
    "<u>Role & title</u>",
)
MARKS = {
    "added": ("+", "Added"),
    "reworded": ("~", "Reworded"),
    "moved": ("↕", "Moved"),
    "removed": ("−", "Removed"),  # noqa: RUF001 - U+2212, the design's minus
    "unchanged": ("=", "Unchanged"),
}
ANNOUNCE = {
    "started": "CV generation started.",
    "edit": "Line saved.",
    "offer": "Line saved. Save this as a fact?",
    "add": "Line added.",
    "remove": "Line removed.",
    "confirm": "Saved as a fact. The line now cites it.",
    "approve": "CV approved.",
    "new_draft": "Saved as a new draft. The approved version is unchanged.",
    "ready": "CV ready to review.",
}


def cv_url(aid: str, action: str) -> str:
    return f"/applications/{aid}/tailoring/3/{action}"


def unit_url(aid: str, uid: str, action: str) -> str:
    return cv_url(aid, f"unit/{uid}/{action}")


def page_url3(aid: str, version: str | None = None) -> str:
    return f"/applications/{aid}?tab=tailoring&step=3" + (f"&version={version}" if version else "")


# --- readers --------------------------------------------------------------------------------------


def step(page: str) -> Element:
    return by_id(page, "cv-step")


def in_step(page: str, predicate: Any) -> list[Element]:
    box = step(page)
    return [e for e in elements(page) if e.within(box) and predicate(e)]


def lines(page: str) -> list[Element]:
    """Every ``li[data-line]`` of the step (tailored lines and removed master lines)."""
    return in_step(page, lambda e: e.tag == "li" and "data-line" in e.attrs)


def tailored_lines(page: str) -> list[Element]:
    return [e for e in lines(page) if "data-unit-id" in e.attrs]


def removed_lines(page: str) -> list[Element]:
    return [e for e in lines(page) if e.attrs.get("data-mark") == "removed"]


def line_by_unit(page: str, unit_id: str) -> Element:
    found = [e for e in tailored_lines(page) if e.attrs.get("data-unit-id") == unit_id]
    assert len(found) == 1, f"expected one line for unit {unit_id}, found {len(found)}"
    return found[0]


def parts(page: str, container: Element, selector_attr: str) -> list[Element]:
    return inside(page, container, lambda e: selector_attr in e.attrs)


def one_part(page: str, container: Element, attr: str) -> Element:
    found = parts(page, container, attr)
    assert len(found) == 1, f"expected one [{attr}], found {len(found)}"
    return found[0]


def mark_text(page: str, line: Element) -> str:
    return one_part(page, line, "data-diff-mark").text


def line_text(page: str, line: Element) -> str:
    return one_part(page, line, "data-line-text").text


def flags_of(page: str, line: Element) -> list[tuple[str, str, str]]:
    """(kind, label, claim) of the line's flags, in order."""
    found: list[tuple[str, str, str]] = []
    for item in inside(page, line, lambda e: e.tag == "li" and "data-flag" in e.attrs):
        label = one_part(page, item, "data-flag-label").text
        claim = one_part(page, item, "data-flag-claim").text
        found.append((str(item.attrs["data-flag"]), label, claim))
    return found


def buttons_in(page: str, container: Element) -> list[str]:
    """The visible names of the buttons and disclosure summaries inside ``container``."""
    return [e.text for e in inside(page, container, lambda e: e.tag in ("button", "summary"))]


def fact_links(page: str, line: Element) -> list[tuple[str, str]]:
    """(href, text) of the cited-fact links of a line."""
    return [
        (str(e.attrs["href"]), e.text)
        for e in inside(page, line, lambda e: e.tag == "a" and "href" in e.attrs)
        if any(a.tag == "ul" and "data-fact-list" in a.attrs for a in e.ancestors())
    ]


def line_form(page: str, line: Element, kind: str) -> Element:
    found = [f for f in forms_in(page, line) if f.attrs.get("data-line-form") == kind]
    assert len(found) == 1, f"expected one {kind} form in the line, found {len(found)}"
    return found[0]


def has_line_form(page: str, line: Element, kind: str) -> bool:
    return any(f.attrs.get("data-line-form") == kind for f in forms_in(page, line))


def version_id(page: str) -> str:
    return str(step(page).attrs["data-version-id"])


def state(page: str) -> str:
    return str(step(page).attrs["data-state"])


def approve_button(page: str) -> Element | None:
    found = [e for e in elements(page) if e.attrs.get("id") == "cv-approve"]
    return found[0] if found else None


def autofocus_elements(page: str) -> list[Element]:
    return [e for e in elements(page) if "data-autofocus" in e.attrs]


def add_form(page: str) -> Element:
    return by_id(page, "cv-add-form")


def control(page: str, container: Element, name: str) -> Element:
    found = inside(
        page,
        container,
        lambda e: (
            e.tag in ("input", "textarea", "select", "button") and e.attrs.get("name") == name
        ),
    )
    assert len(found) >= 1, f"no control {name!r} inside"
    return found[0]


def described_by(page: str, node: Element) -> str:
    """The visible text the control's ``aria-describedby`` points at (joined)."""
    ids = str(node.attrs.get("aria-describedby") or "").split()
    return " ".join(by_id(page, i).text for i in ids)


def assert_escaped(page: str, hostile: str) -> None:
    """The hostile text is shown as text: never raw in the answer, always escaped."""
    assert hostile not in page, f"{hostile!r} appears unescaped"
    assert html.escape(hostile, quote=False) in page, f"{hostile!r} is not shown escaped"


def hidden_inputs(page: str) -> dict[str, str]:
    return {
        str(e.attrs["name"]): html.unescape(str(e.attrs.get("value", "")))
        for e in elements(page)
        if e.tag == "input" and e.attrs.get("type") == "hidden" and e.within(step(page))
    }


# --- the running app ------------------------------------------------------------------------------


class CvStep(Documents):
    """The document harness plus the step-3 routes. ``cv_ready``, ``generate``, ``units`` and the
    rest are inherited."""

    # --- reading -------------------------------------------------------------------------------

    def step3(self, aid: str, version: str | None = None, **headers: str) -> Response:
        """The token-protected fragment (``version``: the older version's route)."""
        path = cv_url(aid, f"version/{version}") if version else f"/applications/{aid}/tailoring/3"
        return self.r.client.get(path, headers=self.r.run.run.htmx(**headers))

    def page3(self, aid: str, version: str | None = None) -> Response:
        """The token-free page."""
        return self.r.client.get(page_url3(aid, version))

    def contact(self, aid: str) -> Response:
        return self.r.client.get(cv_url(aid, "contact"), headers=self.r.run.run.htmx())

    # --- writing --------------------------------------------------------------------------------

    def post_cv(self, aid: str, action: str, values: dict[str, str] | None = None) -> Response:
        return self.post_fields(cv_url(aid, action), values or {})

    def post_unit(
        self, aid: str, uid: str, action: str, values: dict[str, str] | None = None
    ) -> Response:
        return self.post_fields(unit_url(aid, uid, action), values or {})

    def tamper(
        self, sql: str, params: tuple[object, ...] = (), *, triggers: tuple[str, ...] = ()
    ) -> None:
        """Change a stored row the app's own paths never change (TEST ONLY): the named
        "never updated" guards are dropped first, in this test's own temporary database."""
        for name in triggers:
            self.r.run.execute(f"DROP TRIGGER {name}")
        self.r.run.execute(sql, params)

    def version_now(self, aid: str) -> str:
        return version_id(self.step3(aid).text)

    def edit(self, aid: str, uid: str, text: str, *, version: str | None = None) -> Response:
        """Post the edit form as the page draws it (its own ``version``), with ``text``."""
        shown = self.step3(aid).text
        form = line_form(shown, line_by_unit(shown, uid), "edit")
        return self.post_unit(
            aid,
            uid,
            "edit",
            {"version": version or hidden_value(shown, form, "version"), "text": text},
        )

    def remove(self, aid: str, uid: str) -> Response:
        shown = self.step3(aid).text
        form = line_form(shown, line_by_unit(shown, uid), "remove")
        return self.post_unit(aid, uid, "remove", {"version": hidden_value(shown, form, "version")})

    def confirm(self, aid: str, uid: str) -> Response:
        shown = self.step3(aid).text
        form = line_form(shown, line_by_unit(shown, uid), "confirm")
        return self.post_unit(
            aid, uid, "confirm", {"version": hidden_value(shown, form, "version")}
        )

    def add(self, aid: str, section: str, text: str, **extra: str) -> Response:
        shown = self.step3(aid).text
        version = hidden_value(shown, add_form(shown), "version")
        return self.post_cv(
            aid, "unit/add", {"version": version, "section": section, "text": text, **extra}
        )

    def approve_now(self, aid: str) -> Response:
        return self.post_cv(aid, "approve", {"version": self.version_now(aid)})

    def generate_by_route(self, aid: str, fixture: str | None = "ok", **values: str) -> Response:
        """Script the answer, start by the route and wait for the task to end."""
        if fixture is not None:
            self.script("tailor_cv", fixture)
        started = self.post_cv(aid, "generate", values)
        assert started.status_code == 200, (started.status_code, started.text[:300])
        self.ended("tailor_cv", aid)
        return started

    def unit_id(self, aid: str, text: str) -> str:
        """The id of the unit saying ``text`` in the CURRENT version."""
        return str(self.unit_with(self.current_version_id(aid), text)["id"])

    def fact_id(self, number: int) -> str:
        return str(self.fact_row(number)["id"])

    def has(self, page: str, element_id: str) -> bool:
        return has_id(page, element_id)

    def said(self, page: str) -> str:
        return said_text(page)


@contextmanager
def cv_step_app(tmp_path: Any) -> Iterator[CvStep]:
    """Each test file wraps this in its own ``cv`` fixture."""
    with refining(tmp_path) as r:
        yield CvStep(r)
