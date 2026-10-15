"""The Fact bank page's contract (M3.5) and the helpers its integration tests share.

Everything the tests pin about ``/facts`` is named here once: routes, element ids, button names
and copy. Seeding goes through the fact services over the running app's own database, so the
tests need no application code beyond the routes they ask for. Everything is fictional.

Routes (only ``GET /facts`` is a page; every other one needs the launch token, and the POSTs a
matching origin too):

- ``GET /facts?tab=library|drafts|capture`` (plus ``q``, ``role``, ``skill``, ``status`` and
  ``page`` for the Library): the full page, or with ``HX-Request`` the main-content fragment.
- ``GET /facts/library`` (same query fields): the ``#library-results`` fragment.
- ``GET /facts/drafts``: the ``#fact-drafts`` fragment. ``GET /facts/imports``: the
  ``#cv-import-list`` fragment (kept uploads, each with its task's status).
- ``POST /facts/{fact_id}/confirm``, ``/discard``, ``/undo-discard`` (field ``version``) and
  ``/confirm-edited`` (``version`` and the wording: ``text``, or ``situation``, ``action``,
  ``result`` for a story).
- ``POST /facts/import/{import_id}/confirm-all``: one ``version:<fact id>`` field per draft
  of the import, flagged ones included (the service skips them and the answer says so).

An htmx answer to a draft action is a fragment whose ``#fact-<id>`` replaces the one on the
page, with one ``data-autofocus`` target, the words for the live region in
``HX-Trigger: {"announce": ...}`` and the tab link with the new count. The batch answers its
own wrapper (``#draft-group-<import id>-batch``) and removes the blocks of the drafts it
confirmed out of band, touching no other block. A plain POST answers 303 to
``/facts?tab=drafts``.
"""

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from httpx import Response

from tests.cv_import_harness import HTMX, Importing, importing
from tests.fact_bank import MODEL, PROMPT_ID, PROMPT_VERSION, PROVIDER, STAMP, Bank, make_bank
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_setup_redirect import Element, outline

# --- routes ------------------------------------------------------------------------------------

PAGE = "/facts"
LIBRARY_URL = "/facts/library"
DRAFTS_URL = "/facts/drafts"
IMPORTS_URL = "/facts/imports"
IMPORT_URL = "/facts/import"
FRAGMENT_URLS = (LIBRARY_URL, DRAFTS_URL, IMPORTS_URL)
TABS = {"library": "Library", "drafts": "Drafts to review", "capture": "Capture inbox"}
PAGE_SIZE = 25  # ``LibraryQuery.page_size``


def tab_url(tab: str) -> str:
    return f"{PAGE}?tab={tab}"


def confirm_path(fact_id: str) -> str:
    return f"/facts/{fact_id}/confirm"


def edit_confirm_path(fact_id: str) -> str:
    return f"/facts/{fact_id}/confirm-edited"


def discard_path(fact_id: str) -> str:
    return f"/facts/{fact_id}/discard"


def undo_path(fact_id: str) -> str:
    return f"/facts/{fact_id}/undo-discard"


def confirm_all_path(import_id: str) -> str:
    return f"/facts/import/{import_id}/confirm-all"


def batch_field(fact_id: str) -> str:
    return f"version:{fact_id}"


# --- element ids -------------------------------------------------------------------------------

HEADING_ID = "page-heading"
TABS_LABEL = "Fact bank sections"
FILTERS_ID = "library-filters"
RESULTS_ID = "library-results"
COUNT_ID = "library-count"
PAGES_LABEL = "Library pages"
DRAFTS_ID = "fact-drafts"
IMPORT_ID = "cv-import"  # ``IMPORT_TARGET`` of app/api/facts_import.py
IMPORT_FORM_ID = "cv-import-form"
IMPORT_START_ID = "cv-import-start"  # ``IMPORT_FOCUS``
IMPORT_PRIVACY_ID = "cv-import-privacy"
IMPORT_LIST_ID = "cv-import-list"


def fact_id_attr(fact_id: str) -> str:
    return f"fact-{fact_id}"


def group_id(import_id: str) -> str:
    return f"draft-group-{import_id}"


def batch_id(import_id: str) -> str:
    """The wrapper of a group's "Confirm all" form: what the batch's answer replaces."""
    return f"{group_id(import_id)}-batch"


def upload_id(import_id: str) -> str:
    return f"cv-import-{import_id}"


def upload_task_id(import_id: str) -> str:
    """The task's status inside an upload's row (the tasks panel has ``task-<id>``)."""
    return f"{upload_id(import_id)}-task"


IMPORT_SKIP_ID = "cv-import-skip"
IMPORT_PRACTICE_ID = "cv-import-practice"
IMPORT_ERROR_ID = "cv-import-error"
IMPORT_STATUS_ID = "cv-import-status"
STALE_NOTE_ID = "fact-drafts-stale"
DRAFTS_CHANGED = "facts:drafts-changed"


# --- copy --------------------------------------------------------------------------------------

AREA = "Fact bank"
EMPTY_LIBRARY = "No facts yet. Import your CV?"
EMPTY_LINK = "Import your CV"
NO_MATCH = "No facts match your search."
NO_DRAFTS = "No drafts to review."
CAPTURE_LATER = "later version"
PRACTICE_BADGE = "Practice data (fake provider)"
BADGE_NEVER_USE = "Never use in applications"
BADGE_GENERALIZE = "Generalize names"
NEEDS_CHECK_NOTE = (
    "Needs a check: something here wasn't found in your CV. Read it before you confirm."
)
QUOTE_LABEL = "From your CV"
QUOTE_LABEL_UNCHECKED = "What the AI quoted (not checked against your CV)"
DRAFTS_HINT = (
    "A draft is used in a CV or a letter only after you confirm it. "
    "Check each one against the quoted line beside it."
)
PRACTICE_WARNING = (
    "You're using the practice provider. It doesn't read your CV: "
    "you'll get fictional example drafts, labelled Practice data."
)
STARTED_LINE = (
    "CV import started. When it finishes, its drafts are listed under Drafts to review. "
    "If it can't be read, the reason appears under Uploaded CVs."
)
SAID_IMPORT_FAILED = "CV import failed. The reason is under Uploaded CVs."
CHOOSE_ONE = "Choose one: upload a file or paste the text, not both."
PASTE_LABEL = "Or paste your CV's text (leave the file empty)"
REVIEW_LINK = "Review it in Drafts to review"
CANCEL_HINT = "To cancel, open Tasks in the top bar."
DISCARDED_LINE = "Discarded. It won't be used. Undo is available until you leave this page."
STALE_NOTE = "New drafts are ready. Finish what you're editing, then reload the page to see them."
RELOAD = "Reload the page"

CONFIRM = "Confirm"
EDIT = "Edit"
SAVE_CONFIRM = "Save and confirm"
DISCARD = "Discard"
UNDO = "Undo"
CONFIRM_ALL = "Confirm all from this import"
START = "Start"
TRY_AGAIN = "Try again"
REMOVE = "Remove"
IMPORT = "Import CV"
SEARCH = "Search"

SAID_CONFIRMED = "Fact confirmed."
SAID_EDITED = "Fact saved and confirmed."
SAID_DISCARDED = "Draft discarded."
SAID_RESTORED = "Draft restored."
SAID_STARTED = "CV import started."


def drafts_tab_text(count: int) -> str:
    return f"Drafts to review ({count})"


def drafts_phrase(count: int) -> str:
    return "1 draft" if count == 1 else f"{count} drafts"


def empty_with_drafts(count: int) -> str:
    """The Library's empty state while nothing is confirmed but drafts wait."""
    return f"No confirmed facts yet. You have {drafts_phrase(count)} to review."


def skip_text(count: int) -> str:
    return f"Skip to drafts ({count})"


def batch_hint(count: int) -> str:
    """The sentence above "Confirm all from this import": what it would confirm."""
    left = "Drafts marked 'Needs a check' are left for you."
    if count == 1:
        return f"Confirms the 1 draft in this import that needs no check. {left}"
    return f"Confirms the {count} drafts in this import that need no check. {left}"


def found_said(total: int) -> str:
    """What a Library search tells the live region."""
    if total == 0:
        return NO_MATCH
    return "1 fact found." if total == 1 else f"{total} facts found."


def batch_said(confirmed: int, skipped: int) -> str:
    """The batch button's result (ADR-091: it must say what it left)."""
    if confirmed == 0:
        first = "No facts confirmed."
    else:
        first = "1 fact confirmed." if confirmed == 1 else f"{confirmed} facts confirmed."
    if skipped == 0:
        return first
    if skipped == 1:
        return f"{first} 1 draft needs a check and was left for you to confirm on its own."
    return f"{first} {skipped} drafts need a check and were left for you to confirm one by one."


# --- the running app ---------------------------------------------------------------------------


class FactsApp:
    """The running app (finished setup, the fake provider saved) with the fact services over
    its own database for seeding and reading."""

    def __init__(self, run: Importing) -> None:
        self.run = run
        self.db = run.app.state.workspace.db
        self.bank: Bank = make_bank(self.db, run.clock, SequentialIdFactory("seed"))

    @property
    def token(self) -> str:
        return self.run.run.token

    def get(self, path: str, *, htmx: bool = True, **params: str) -> Response:
        headers = self.run.run.htmx() if htmx else {}
        return self.run.client.get(path, params=params, headers=headers)

    def fragment(self, path: str, **params: str) -> list[Element]:
        """A fragment route's answer (token, htmx), parsed."""
        response = self.get(path, **params)
        assert response.status_code == 200, (path, response.status_code, response.text[:300])
        assert "<html" not in response.text, f"{path} answers a fragment"
        return outline(response.text)

    def tab(self, tab: str, **params: str) -> list[Element]:
        """The full page on ``tab``, parsed."""
        response = self.get(PAGE, htmx=False, tab=tab, **params)
        assert response.status_code == 200, (tab, response.status_code, response.text[:300])
        assert "<html" in response.text
        return outline(response.text)

    def post(self, path: str, data: dict[str, str] | None = None, *, htmx: bool = True) -> Response:
        headers = self.run.run.action(**(HTMX if htmx else {}))
        return self.run.client.post(path, data=data or {}, headers=headers)

    def act(self, path: str, fact_id: str, *, htmx: bool = True, **data: str) -> Response:
        """Post a draft action with the fact's current version."""
        version = str(self.bank.facts.get(fact_id).version)
        return self.post(path, {"version": version, **data}, htmx=htmx)

    def confirm_all(
        self, import_id: str, fact_ids: Sequence[str], *, htmx: bool = True
    ) -> Response:
        data = {batch_field(i): str(self.bank.facts.get(i).version) for i in fact_ids}
        return self.post(confirm_all_path(import_id), data, htmx=htmx)

    def status(self, fact_id: str) -> str:
        return str(self.bank.facts.get(fact_id).status.value)

    def snapshot(self) -> str:
        """Every row of the database, as text."""
        with self.db.transaction() as conn:
            return "\n".join(conn.iterdump())

    def add_import(self, import_id: str, *, state: str = "done") -> str:
        """An import row written directly (no file): ``done``, or ``uploaded`` and never
        started."""
        with self.db.transaction() as conn:
            _insert_import(conn, import_id, state)
        return import_id

    def drafts(self, import_id: str | None, *drafts: Any, role: Any = None) -> list[Any]:
        """Drafts as the CV import makes them (provider ``fake``), from ``DraftInput`` values
        or plain wordings."""
        from app.services.facts import DraftInput, DraftOrigin

        origin = DraftOrigin(
            source="cv_import",
            provider=PROVIDER,
            model=MODEL,
            prompt_id=PROMPT_ID,
            prompt_version=PROMPT_VERSION,
            cv_import_id=import_id,
        )
        made = [
            DraftInput(text=d, original_answer=f"CV line: {d}") if isinstance(d, str) else d
            for d in drafts
        ]
        roles = [] if role is None else [role]
        return list(self.bank.facts.create_drafts(origin, made, roles=roles))


def _insert_import(conn: sqlite3.Connection, import_id: str, state: str) -> None:
    conn.execute(
        "INSERT INTO cv_imports (id, kind, stored_name, state, task_id, created_at, "
        "updated_at, version) VALUES (?, 'text', ?, ?, NULL, ?, ?, 1)",
        (import_id, f"{import_id}.txt", state, STAMP, STAMP),
    )


@contextmanager
def facts_app(tmp_path: Path) -> Iterator[FactsApp]:
    with importing(tmp_path) as run:
        yield FactsApp(run)


# --- reading answers ---------------------------------------------------------------------------


def inside(elements: list[Element], parent: Element) -> list[Element]:
    return [e for e in elements if e.within(parent)]


def by_id(elements: list[Element], element_id: str) -> Element | None:
    found = [e for e in elements if e.attrs.get("id") == element_id]
    assert len(found) <= 1, f"#{element_id} appears {len(found)} times"
    return found[0] if found else None


def required(elements: list[Element], element_id: str) -> Element:
    element = by_id(elements, element_id)
    assert element is not None, f"#{element_id} is missing"
    return element


def texts(elements: list[Element], parent: Element | None = None) -> set[str]:
    """The text of every element (inside ``parent``, with ``parent`` itself)."""
    if parent is None:
        return {e.text for e in elements}
    return {e.text for e in inside(elements, parent)} | {parent.text}


def says(elements: list[Element], text: str, parent: Element | None = None) -> bool:
    """Whether some element's whole text is ``text`` (so it is text, not an attribute)."""
    return text in texts(elements, parent)


def says_ignoring_case(elements: list[Element], text: str, parent: Element) -> bool:
    return text.lower() in {found.lower() for found in texts(elements, parent)}


def buttons(elements: list[Element], parent: Element, name: str) -> list[Element]:
    return [e for e in inside(elements, parent) if e.tag == "button" and e.text == name]


def button(elements: list[Element], parent: Element, name: str) -> Element:
    found = buttons(elements, parent, name)
    assert len(found) == 1, f'expected one "{name}" button in #{parent.attrs.get("id")}'
    return found[0]


def links(elements: list[Element], name: str, parent: Element | None = None) -> list[Element]:
    pool = elements if parent is None else inside(elements, parent)
    return [e for e in pool if e.tag == "a" and e.text == name]


def href_parts(link: Element) -> tuple[str, dict[str, str], str]:
    """A link's path, its query (one value per name) and its #fragment."""
    parts = urlsplit(link.attrs.get("href") or "")
    query = {name: values[-1] for name, values in parse_qs(parts.query).items()}
    return parts.path, query, parts.fragment


def named_control(elements: list[Element], form: Element, name: str) -> Element:
    found = [e for e in inside(elements, form) if e.attrs.get("name") == name]
    assert len(found) == 1, f'expected one control named "{name}", found {len(found)}'
    return found[0]


def has_label(elements: list[Element], control: Element) -> bool:
    """A visible ``<label for>`` with text names the control."""
    control_id = control.attrs.get("id")
    return bool(control_id) and any(
        e.tag == "label" and e.attrs.get("for") == control_id and e.text for e in elements
    )


def option_values(elements: list[Element], select: Element) -> dict[str, str]:
    """A select's options: value to text."""
    return {
        e.attrs.get("value") or "": e.text for e in inside(elements, select) if e.tag == "option"
    }


def focus_targets(elements: list[Element]) -> list[Element]:
    return [e for e in elements if "data-autofocus" in e.attrs]


def only_focus(elements: list[Element]) -> Element:
    found = focus_targets(elements)
    assert len(found) == 1, f"expected one data-autofocus target, found {len(found)}"
    return found[0]


def announcement(response: Response) -> str:
    """The words for the live region (``HX-Trigger: {"announce": ...}``); other events may
    travel in the same header."""
    assert "hx-trigger" in response.headers, "the answer announces its result"
    trigger = json.loads(response.headers["hx-trigger"])
    assert isinstance(trigger, dict) and isinstance(trigger.get("announce"), str), trigger
    said: str = trigger["announce"]
    return said


def fact_ids_shown(
    elements: list[Element], known: Sequence[str], parent: Element | None = None
) -> list[str]:
    """Which of the ``known`` facts an answer lists (elements with id ``fact-<id>``), in the
    order they appear."""
    pool = elements if parent is None else inside(elements, parent)
    wanted = {fact_id_attr(fact_id): fact_id for fact_id in known}
    return [wanted[i] for e in pool if (i := e.attrs.get("id") or "") in wanted]


# ================================================================================================
# M3.6: the editor, the history and the merge screen (AC-M3.2, AC-M3.3)
# ================================================================================================
#
# Pages (registered as pages: no token needed, full HTML, GET and HEAD change nothing):
#
# - ``GET /facts/new`` (``?type=fact|story``; anything else is a fact): the editor for a new item.
# - ``GET /facts/{id}/edit``: the editor for one fact, with its history below (``#fact-history``).
# - ``GET /facts/{draft id}/merge`` (``?q=`` searches, ``?target=<fact id>`` compares): the merge
#   screen of one draft; for a draft a merge closed it shows the result and, while it can be
#   taken back, "Undo merge".
#
# Everything else needs the launch token, and each POST the app's own origin too:
#
# - ``GET /facts/{id}/history``: the ``#fact-history`` fragment.
# - ``POST /facts/new`` and ``POST /facts/{id}/edit`` (the editor's fields; the second with
#   ``version``). ``POST /facts/{id}/archive`` and ``/restore`` (``version``).
# - ``POST /facts/{draft id}/merge`` (``target``, ``draft_version``, ``target_version``,
#   ``wording`` = ``target|draft|edited`` and, for ``edited``, the wording fields) and
#   ``POST /facts/{draft id}/undo-merge`` (``draft_version``, ``target_version``).
# - A discarded draft is restored with the existing ``POST /facts/{id}/undo-discard``, from the
#   Library's "Discarded" filter or from its own page, with the hidden field ``from``
#   (``library`` or ``editor``): the answer, a 409's link and a plain 303 lead back there.
#
# An htmx answer is a fragment with one ``data-autofocus`` target and the words for the live
# region; a plain POST answers 303. 404 unknown fact, 409 stale or refused (reload prompt), 422
# refused input tied to its field.

NEW_URL = "/facts/new"
TYPE_FIELD = "type"
WORDING_FIELDS = {"fact": ("text",), "story": ("situation", "action", "result")}
# The editor's other fields. ``skills`` is one checkbox per skill of the user's list (its value
# is the skill's name); ``new_skills`` and ``tags`` are comma-separated; a new role is typed as
# employer and title and wins over the chosen one.
ROLE_FIELD = "role_id"
NEW_ROLE_FIELDS = ("new_role_employer", "new_role_title")
SKILLS_FIELD = "skills"
NEW_SKILLS_FIELD = "new_skills"
TAGS_FIELD = "tags"
DATE_FIELDS = ("start_date", "end_date")
PRIVACY_FIELD = "privacy"


def new_url(kind: str = "fact") -> str:
    return NEW_URL if kind == "fact" else f"{NEW_URL}?type={kind}"


def edit_url(fact_id: str) -> str:
    return f"/facts/{fact_id}/edit"


def history_url(fact_id: str) -> str:
    return f"/facts/{fact_id}/history"


def archive_path(fact_id: str) -> str:
    return f"/facts/{fact_id}/archive"


def restore_path(fact_id: str) -> str:
    return f"/facts/{fact_id}/restore"


def merge_url(draft_id: str) -> str:
    return f"/facts/{draft_id}/merge"


def undo_merge_path(draft_id: str) -> str:
    return f"/facts/{draft_id}/undo-merge"


# --- element ids ---

EDITOR_ID = "fact-editor"  # the editor's <form>
EDITOR_STATUS_ID = "fact-editor-status"  # the line a save leaves ("Fact saved.")
EDITOR_CONFLICT_ID = "fact-editor-conflict"  # the reload prompt of a stale save
EDITOR_PAGE_ID = "fact-editor-page"  # the editor's content: what a save replaces
EDITOR_DRAFT_ID = "fact-editor-draft"  # under a draft's heading: what it is and what made it
EDITOR_STATE_STATUS_ID = "fact-editor-state-status"  # the line Archive or Restore leaves
EDITOR_KEPT_ID = "fact-editor-kept"  # what a refused save typed for a fact with no form now
MERGE_CONTENT_ID = "merge-content"  # the merge screen's content: what its search replaces
MERGE_CANDIDATES_HEADING_ID = "merge-candidates-heading"
HISTORY_ID = "fact-history"
HISTORY_EVENTS_ID = "fact-history-events"
HISTORY_VERSIONS_ID = "fact-history-versions"
MERGE_SEARCH_ID = "merge-search"  # a plain GET form, field ``q``
MERGE_CANDIDATES_ID = "merge-candidates"
MERGE_COMPARE_ID = "merge-compare"
MERGE_DRAFT_ID = "merge-draft"  # the draft's side of the comparison
MERGE_TARGET_ID = "merge-target"  # the other fact's side
MERGE_FORM_ID = "merge-form"
MERGE_RESULT_ID = "merge-result"
MERGE_CONFLICT_ID = "merge-conflict"


def wording_id(fact_id: str) -> str:
    """The element holding a fact's wording where it is listed (M3.5's ``_fact_body``)."""
    return f"wording-{fact_id}"


def version_id_attr(number: int) -> str:
    """One version's entry in the history; ``number`` is the 3 of ``F12@3``."""
    return f"fact-version-{number}"


def merge_candidate_id(fact_id: str) -> str:
    return f"merge-candidate-{fact_id}"


# --- copy ---

ADD_FACT = "Add a fact"
ADD_STORY = "Add a story"
NEW_HEADINGS = {"fact": "Add a fact", "story": "Add a story"}
EDIT_HEADINGS = {"fact": "Edit fact", "story": "Edit story"}
SAVE = "Save"
EDIT_DETAILS = "Edit details"  # a draft's link to the full editor ("Edit" is its inline form)
HISTORY = "History"
MERGE_WITH = "Merge with…"
ARCHIVE = "Archive"
RESTORE = "Restore"
RESTORE_DRAFT = "Restore draft"
PRIVACY_LEGEND = "Privacy"
PRIVACY_LABELS = {
    "normal": "Normal",
    "never_use": BADGE_NEVER_USE,
    "generalize": BADGE_GENERALIZE,
}
PRIVACY_HELP = {
    "normal": "The app may use this in your CVs and letters and send it to your AI provider.",
    "never_use": (
        "Kept for your own reference. It is never put in a CV or a letter and never sent to the AI."
    ),
    "generalize": (
        "The AI is told to describe names in general terms, such as "
        '"a large retail bank" instead of a client\'s name.'
    ),
}
END_BEFORE_START = "The end date is before the start date."
DATE_HINT = (
    "A year (2024), a month (2024-03) or a full date (2024-03-15). Leave empty if it doesn't apply."
)
DATE_ERROR = "Write the date as 2024, 2024-03 or 2024-03-15."
SAID_ADDED = "Fact added to your Library."
SAID_SAVED = "Fact saved."
SAID_STORY_ADDED = "Story added to your Library."
SAID_STORY_SAVED = "Story saved."
# A save in the full editor never confirms a draft, and its line must not read as if it did.
SAID_DRAFT_SAVED = "Draft saved. It still needs your confirmation in Drafts to review."
STORY_EMPTY = "Fill in at least one of situation, action or result."
STORY_INTRO = "Fill in what you have; you can add the rest later."
STORY_HINTS = {
    "situation": "What was going on, in a sentence or two.",
    "action": "What you did yourself.",
    "result": "What changed, with a number if you have one.",
}
BACK_TO_LIBRARY = "Back to the Library"
BACK_TO_DRAFTS = "Back to Drafts to review"
CLOSED_HEADINGS = {"discarded": "Discarded draft", "merged": "Merged draft"}
# "Restore draft" outside the Drafts tab says where it was pressed.
FROM_FIELD = "from"
DISCARDED_LIBRARY = ("/facts", {"tab": "library", "status": "discarded"})
TITLE_SUFFIX = " · Fact bank · Job Search Pipeline"
# The longest names and lists the editor takes.
MAX_NAME_CHARS = 80
MAX_ROLE_CHARS = 200
MAX_TAGS = 30
MAX_SKILLS = 60
SAID_ARCHIVED = "Fact archived."
SAID_UNARCHIVED = "Fact restored."

# History: newest first, for the events and for the versions.
VERSION_SOURCES = {
    "manual": "Written by you",
    "cv_import": "From a CV import",
    "merge": "From a merge",
    "merge_undo": "Merge undone",
}
EVENT_LABELS = {
    "created": "Created",
    "confirmed": "Confirmed",
    "edited": "Wording changed",
    "details_changed": "Details changed",
    "archived": "Archived",
    "restored": "Restored",
    "discarded": "Discarded",
    "discard_undone": "Discard undone",
    "merged": "A draft was merged into this fact",
    "merged_into": "Merged into another fact",
    "merge_undone": "Merge undone",
}
# What a screen reader hears before a changed run of words: the first word inside each
# ``<ins>`` and ``<del>`` (hidden from sight, never from assistive technology).
DIFF_ADDED = "added"
DIFF_REMOVED = "removed"
# And after it, hidden the same way, so a run's end is heard too.
DIFF_END_ADDED = "(end added)"
DIFF_END_REMOVED = "(end removed)"
DIFF_SPACING_ONLY = "Only spacing changed."


def version_heading(number: int) -> str:
    return f"Version {number}"


def privacy_event(privacy: str) -> str:
    return f"Privacy changed to {PRIVACY_LABELS[privacy]}"


MERGE_HEADING = "Merge with another fact"
COMPARE = "Compare"  # each candidate's link to the comparison
MERGE_CHOICE_FIELD = "wording"
MERGE_CHOICES = {  # in this order; the first is chosen when the comparison opens
    "target": "Keep the existing fact's wording",
    "draft": "Use the draft's wording",
    "edited": "Write the wording myself",
}
# The same choices when the other side is a draft too: nothing is an "existing fact" yet.
MERGE_CHOICES_DRAFT_TARGET = {**MERGE_CHOICES, "target": "Keep the other draft's wording"}
MERGE_LEGEND = "Which wording should the fact keep?"
MERGE_LEGEND_DRAFT_TARGET = "Which wording should be kept?"
MERGE_TYPED_NOT_CHOSEN = (
    'You changed the wording below. Choose "Write the wording myself" to use it, '
    "or undo your change."
)
MERGE_WORDING_NEEDED = "Write the wording before merging."
MERGE_NOT_A_DRAFT = "This fact isn't a draft any more, so it can't be merged from here."
MERGE_UNDO_BLOCKED = (
    "This merge can't be undone right now: the fact's wording changed after it. "
    "If another draft was merged into it later, undo that merge first."
)
MERGE_CAPPED = "Not every fact is listed. Search to find the one you want."
MERGE = "Merge"
UNDO_MERGE = "Undo merge"
SAID_MERGED = "Draft merged into the fact."
SAID_MERGE_UNDONE = "Merge undone. The draft is back in Drafts to review."


# --- reading forms ---

FormValues = dict[str, str | list[str]]


def send(facts: FactsApp, path: str, data: Mapping[str, object], *, htmx: bool = True) -> Response:
    """Post form fields, some with several values (checkboxes), with token and origin."""
    headers = facts.run.run.action(**(HTMX if htmx else {}))
    return facts.run.client.post(path, data=dict(data), headers=headers)


def page_of(facts: FactsApp, path: str, **params: str) -> list[Element]:
    """A page route's answer with no token at all, parsed: a full page."""
    response = facts.run.client.get(path, params=params)
    assert response.status_code == 200, (path, response.status_code, response.text[:300])
    assert "<html" in response.text, f"{path} is a page"
    return outline(response.text)


def controls(elements: list[Element], form: Element, name: str) -> list[Element]:
    """The form's controls called ``name`` (several for radios and checkboxes)."""
    form_id = form.attrs.get("id")
    return [
        e
        for e in elements
        if e.attrs.get("name") == name
        and e.tag in ("input", "select", "textarea")
        and (e.within(form) or (form_id and e.attrs.get("form") == form_id))
    ]


def form_values(elements: list[Element], form: Element) -> FormValues:
    """What the form would send as it stands: text fields, the chosen option of each select,
    the checked radio and every checked checkbox (a list)."""
    form_id = form.attrs.get("id")
    values: FormValues = {}
    for element in elements:
        name = element.attrs.get("name")
        owned = element.within(form) or (form_id and element.attrs.get("form") == form_id)
        if not name or not owned:
            continue
        kind = element.attrs.get("type") or "text"
        if element.tag == "textarea":
            values[name] = element.text
        elif element.tag == "select":
            options = [o for o in elements if o.tag == "option" and o.within(element)]
            chosen = next((o for o in options if "selected" in o.attrs), options[0])
            values[name] = chosen.attrs.get("value") or ""
        elif element.tag != "input" or kind in ("submit", "button", "file"):
            continue
        elif kind == "checkbox":
            listed = values.setdefault(name, [])
            assert isinstance(listed, list)
            if "checked" in element.attrs:
                listed.append(element.attrs.get("value") or "on")
        elif kind == "radio":
            if "checked" in element.attrs:
                values[name] = element.attrs.get("value") or ""
        else:
            values[name] = element.attrs.get("value") or ""
    return values


def posts_to(form: Element) -> str:
    """Where a form posts: the same for htmx and without scripts."""
    assert (form.attrs.get("method") or "").lower() == "post", form.attrs
    path = form.attrs.get("action")
    assert path and form.attrs.get("hx-post") == path, form.attrs
    return path


def described_by(elements: list[Element], control: Element) -> list[Element]:
    return [required(elements, i) for i in (control.attrs.get("aria-describedby") or "").split()]


def label_of(elements: list[Element], control: Element) -> str:
    """The text of the control's visible ``<label for>``."""
    control_id = control.attrs.get("id")
    found = [e.text for e in elements if e.tag == "label" and e.attrs.get("for") == control_id]
    assert control_id and len(found) == 1, (control.attrs, found)
    return found[0]


def names_fact(elements: list[Element], control: Element, wording: str) -> bool:
    """Whether a link or button says which fact it is for: its name holds the fact's wording,
    or it is labelled or described by an element that does."""
    if wording in control.text or wording in (control.attrs.get("aria-label") or ""):
        return True
    ids = [
        i
        for attr in ("aria-describedby", "aria-labelledby")
        for i in (control.attrs.get(attr) or "").split()
    ]
    return any(wording in (found.text if (found := by_id(elements, i)) else "") for i in ids)


def headings(elements: list[Element], tag: str) -> list[str]:
    return [e.text for e in elements if e.tag == tag]


def alert(elements: list[Element], prompt_id: str) -> Element:
    """The one alert of a 409 answer."""
    alerts = [e for e in elements if e.attrs.get("role") == "alert"]
    assert len(alerts) == 1, f"expected one alert, found {len(alerts)}"
    assert alerts[0].attrs.get("id") == prompt_id, alerts[0].attrs
    return alerts[0]
