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
from collections.abc import Iterator, Sequence
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
