"""The running app, assumed interface, markup hooks and readers for the M6.10 tests (exports, the
Submit step 5 of the Tailoring tab and the freeze on Applied; AC-M6.5 photo in exports, AC-M6.6,
AC-M6.7 freeze; ADR-102 item 8, ADR-106 items 3 and 10, ADR-110 item 3 and its consequence, ADR-112
items 9 and 11, ADR-113, ADR-114, ADR-115; spec 2.9 rules 2, 12 and 13). Every value is fictional.
No application module of M6.10 is imported at module level, so a missing service, route or
template fails the test that needs it, not the collection. The renderers are the real ones (typst
and python-docx) unless a test wraps them in a ``Probe``; the data is fictional.

ASSUMED INTERFACE (the implementer follows it; where the task text was silent the choice is marked
"CHOICE")

Wiring and service
- ``Wiring.document_renderers() -> tuple[DocumentRenderer, ...]`` in ``app/wiring.py`` (like
  ``calendar_provider``): ``(PdfRenderer(self.clock), WordRenderer(self.clock))``. ``create_app``
  calls it once and builds ``app.state.exports = ExportService(...)`` (``app/services/exports.py``)
  from it, keyed by each renderer's ``format`` (``"pdf"``, ``"docx"``). The harness replaces the
  method through a wiring proxy to inject ``Probe``-wrapped or failing renderers.
- ``ExportService.export(application_id)`` is a coroutine: it reads in a thread, renders each file
  with ``asyncio.to_thread(renderer.render, document)`` (so a render never runs with the event loop
  in its thread), then writes the files and the rows. Every route below calls it; tests observe it
  only through routes, files and tables.
- Constants in ``app.services.exports`` (the tests compare messages with them):
  ``NO_NAME`` (shown when ``personal_details`` has no name; mentions the name and Settings) and
  ``NOT_APPROVED`` (shown when the CV has no approved current version; mentions approving the CV).
  Both are plain sentences with no class names. A refusal is a 409 and writes nothing.

What Export does (``POST /applications/{id}/tailoring/5/export``, no fields; any field is the
plain-text 400 ``INVALID_REQUEST`` "This request isn't valid.")
- Needs a saved name and an APPROVED current CV version (status ``approved`` or ``sent``). The
  cover letter is exported only when its current version is approved (or sent); an unapproved
  letter draft is left out and does not refuse the export. CHOICE.
- For each such version it renders both formats: the CV (``RenderDocument`` built from the version's
  visible units, the template SNAPSHOT of that version (headings, date format, details shown, page
  size) and the saved ``personal_details``; the photo only when the snapshot has ``photo_slot``
  AND a
  photo is saved) and the letter (code-built frame around the paragraphs; no photo ever).
- Writes ``<data>/documents/<application id>/`` (mode 0700, never through a link) and in it, for
  each
  rendered file, a regular file created exclusively with mode 0600, fsynced, plus ONE row of
  ``document_exports`` (``document_version_id``, ``format``, ``stored_name``, ``sha256`` of the
  bytes,
  ``size_bytes``, ``pages`` = the PDF's page count and NULL for Word).
- Names are built by code from the kind and the version number only: ``cv-v<number>`` or
  ``cover-letter-v<number>``, an OPTIONAL ``-<[a-z0-9]+>`` suffix (CHOICE: the schema keeps
  ``stored_name`` unique across ALL applications, so two applications' ``cv-v1.pdf`` would collide;
  the implementer may add a short code-built suffix from ids), then ``.pdf`` or ``.docx``. That
  shape is ``GENERATED_NAME`` below and is also the definition of a generated name for the orphan
  sweep. No user text (title, company, name) is ever in a name.
- A version that already has both files is not rendered again (reuse): the renderers are not called
  for it and no row or file is added. A new approved version gets new files; the old files and rows
  stay (they may have been sent).
- All or nothing: if any render raises (``RenderFailed``, ``RenderTooLarge``, ``RenderTimedOut``) or
  any write fails, no file of this export remains and no row is written (a 409 with a plain user
  message; the folder may exist but is empty of this export's files). ``RendererUnavailable`` is the
  exception: the formats that could render are kept (Word), the unavailable one is skipped, the
  answer is 200 and holds ``p#submit-unavailable`` with the exception's own message, announced too.

Download (``GET /applications/{id}/exports/{export_id}``, token needed, not a page)
- The file path comes only from the row (``documents/<application id>/<stored_name>``), checked to
  be a regular file inside the application's folder with no link on the way (``O_NOFOLLOW``-style);
  anything else is the fixed plain-text 404 ``DOWNLOAD_NOT_FOUND`` below, echoing nothing. The
  export
  must belong to that application. Answer: 200, the file's bytes, ``Content-Type``
  ``application/pdf``
  or the Word media type, ``Content-Disposition: attachment; filename="<stored_name>"``,
  ``Cache-Control: no-store``. It writes nothing. Other methods are 405.
- The browser fetches it with the delegated helper of ``app.js`` (extends the M5.5 .ics exception):
  a ``button[data-file-download="/applications/{id}/exports/{export_id}"]`` (CHOICE of attribute
  name; the path pattern is checked by the helper), the token in a header, never in the URL.

Step 5 (Submit), markup inside ``#tailoring[data-step="5"]``; stepper title "Submit"; the heading
"Step 5: Submit"; "Coming later" and "later step" text are gone
- ``section#submit-step`` with ``data-applied="true|false"`` and ``h5#submit-heading[tabindex=-1]``
  "Submit".
- ``ul#submit-checklist > li[data-check][data-done="true|false"]`` for ``details``, ``cv``,
  ``letter``, ``files`` and ``pages`` (this order). Each shows its state in words (not colour
  alone). Texts: details: "Your details" with a link to ``/settings`` when not done (done when a
  name is saved); cv: "CV approved" / "CV not approved yet" with a link to step 3 when not done
  (done when the current CV version is approved or sent); letter: "Cover letter approved" /
  "Cover letter not approved yet" with a link to step 4 / and "No cover letter" (done) when the
  application has no letter document at all (CHOICE: the letter is skipped); files: "Files
  exported" (done when the version(s) that would be exported have both files) / "Files not exported
  yet"; pages: "<N> page(s)" and the template's maximum, done when N <= max_pages; before an export
  it says the count is known after exporting (not done).
- ``p#submit-export-reason`` (visible reason) and ``button#submit-export`` "Export files" with
  ``aria-disabled="true"`` and ``aria-describedby="submit-export-reason"`` while no name or no
  approved CV (the server refuses the same with 409); otherwise enabled. ``form#submit-export-form``
  posts to the export route; the button is the form's submit.
- ``ul#submit-files > li[data-file][data-document="cv|cover_letter"][data-format="pdf|docx"]`` for
  each export of the shown versions, holding the stored name as text and a
  ``button[data-file-download]`` named "Download CV (PDF)", "Download CV (Word)", "Download cover
  letter (PDF)" or "Download cover letter (Word)"; ``p#submit-no-files`` while there are none.
- ``a#submit-posting-link`` (the job's first saved http/https source URL, ``rel`` contains
  ``noopener``) or ``p#submit-no-posting`` when the job has no link. CHOICE.
- ``form#submit-applied-form`` (POST ``.../tailoring/5/applied``, hidden ``version`` = the
  application's version, no other field) with ``button#submit-mark-applied`` "I've submitted it —
  mark Applied" while ``applied_at`` is empty. It is NOT disabled by an incomplete checklist (the
  user
  submits personally; the checklist advises). After the move: no form, ``p#submit-applied`` says it
  is marked Applied, ``data-applied="true"``. CHOICE.
- ``POST .../tailoring/5/applied`` moves the application to the built-in ``applied`` status,
  independently of the user's column order, with ``ApplicationService.move`` (the freeze
  happens inside it) and the history source ``drawer`` (an existing source; no migration). A repeat
  when ``applied_at`` is set is a 409 with ``p#submit-error`` and writes nothing. User-approved
  clarification after independent review (2026-10-28).
- Answers: 200 ``#tailoring`` fragment with ``HX-Trigger`` ``{"announce": ...}`` "Files exported."
  or "Moved to Applied."; exactly one ``data-autofocus`` (``#submit-heading``). 409: the step again
  with ``p#submit-error[role=alert]`` holding the message; a stale application ``version`` is a 409
  with ``#submit-conflict`` (the shared reload prompt). A missing, malformed or extra field is a
  400.
- ``form#country-form`` (the CV country select, M6.6) stays on step 5 as on every step:
  ``test_application_country.py`` reads it there. CHOICE.
- ``GET /applications/{id}?tab=tailoring&step=5`` is the token-free page (reads only, writes
  nothing, no personal detail: only file names, the checklist and links); ``GET
  /applications/{id}/tailoring/5`` is the fragment (token). An unsafe or unknown id is the fixed
  404 ``ApplicationNotFound.message``.

The freeze (``ApplicationService.move``, the same transaction as ``applied_at``)
- On the first move into an active-phase status (``applied_at`` is empty), whatever the source
  (board, menu, drawer or the Submit step), it writes ONE ``application_submissions`` row
  (``frozen_at`` = the move's instant = ``applied_at``) naming, per document, the latest APPROVED
  version that has an export; else the latest approved (or already sent) version; else NULL. Each
  named version's status becomes ``sent`` (``tailoring_repo.set_version_status``). With no document
  at all the row is still written, with both versions NULL. CHOICE.
- Later moves (another status, back to Preparing and Applied again) write nothing more. A move to a
  closed status from Preparing does not freeze (no ``applied_at``). A failing freeze rolls back the
  whole move (status, history, ``applied_at``).
- A draft version is never sent. Editing after the freeze makes a new draft version (M6.7 rules);
  the sent version, its units, citations and exports never change.

Orphan sweep: ``sweep_orphan_exports(data_dir, db)`` (like ``sweep_orphan_photos``) runs when a
workspace opens (``app/main.py``): it removes regular files directly inside
``documents/<application folder>/`` whose name matches ``GENERATED_NAME`` and that no
``document_exports`` row of that application names. Never a link, a folder, another name, a file in
a deeper folder or a file a row names; a link anywhere on the path (``documents`` or the application
folder) means nothing is swept there.
"""

import asyncio
import hashlib
import importlib
import os
import re
import stat
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from httpx import Response

from app.main import create_app
from app.render import RendererUnavailable
from tests.cv_import_harness import DEADLINE_S, Hold, Importing, SpyProvider, SpyWiring
from tests.details_pages import Desk as DetailsDesk
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_practice_task import START
from tests.integration.test_setup_provider import make_wiring
from tests.integration.test_setup_redirect import BASE_URL, Running
from tests.integration.test_setup_reopen_banner import finished_guide, make_config
from tests.letter_pages import LetterDesk
from tests.refine_harness import Refining
from tests.render_support import (
    BIDI_CONTROLS,
    ZERO_WIDTH,
    docx_text,
    pdf_text,
)
from tests.tailoring_pages import by_id, inside
from tests.template_pages import real_png

DOWNLOAD_NOT_FOUND = "That file wasn't found."
INVALID = "This request isn't valid."
GENERATED_NAME = re.compile(r"^(cv|cover-letter)-v(\d+)(-[a-z0-9]+)?\.(pdf|docx)$")
WORD_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MEDIA = {"pdf": "application/pdf", "docx": WORD_TYPE}
PDF_UNAVAILABLE = "PDF export needs fakepdf. Word export still works; see the README."
EXPORT_ANNOUNCE = "Files exported."
APPLIED_ANNOUNCE = "Moved to Applied."
MARK_APPLIED = "I've submitted it — mark Applied"
POSTING_URL = "https://jobs.example.test/route-planner-4821"
# What may stand between the pieces of a document that the test knows: separators, the word a
# current role's end may show, and a role line's "at".
FILLER = re.compile(r"(?:[-\u2013\u2014\u2212\u00b7\u2022|,/()@:;.]|present|current|at)*", re.I)
BUTTON_NAMES = {
    ("cv", "pdf"): "Download CV (PDF)",
    ("cv", "docx"): "Download CV (Word)",
    ("cover_letter", "pdf"): "Download cover letter (PDF)",
    ("cover_letter", "docx"): "Download cover letter (Word)",
}


def step5_url(aid: str) -> str:
    return f"/applications/{aid}/tailoring/5"


def step5_page_url(aid: str) -> str:
    return f"/applications/{aid}?tab=tailoring&step=5"


def export_url(aid: str) -> str:
    return step5_url(aid) + "/export"


def applied_url(aid: str) -> str:
    return step5_url(aid) + "/applied"


def download_url(aid: str, export_id: str) -> str:
    # Keep dot segments intact until the server decodes them; clients otherwise normalize '..'.
    encoded = export_id.replace(".", "%2E")
    return f"/applications/{aid}/exports/{encoded}"


def exports_module() -> Any:
    return importlib.import_module("app.services.exports")


# --- readers of the step -------------------------------------------------------------------------


def submit_step(page: str) -> Any:
    return by_id(page, "submit-step")


def checklist(page: str) -> dict[str, Any]:
    """``{check: element}`` of ``ul#submit-checklist``, in page order."""
    box = by_id(page, "submit-checklist")
    items = inside(page, box, lambda e: e.tag == "li" and "data-check" in e.attrs)
    return {str(e.attrs["data-check"]): e for e in items}


def done_flags(page: str) -> dict[str, str]:
    return {name: str(item.attrs["data-done"]) for name, item in checklist(page).items()}


def file_items(page: str) -> list[Any]:
    box = by_id(page, "submit-files")
    return inside(page, box, lambda e: e.tag == "li" and "data-file" in e.attrs)


def download_buttons(page: str) -> dict[tuple[str, str], tuple[str, str]]:
    """``{(document, format): (button text, path)}`` of the file list."""
    found: dict[tuple[str, str], tuple[str, str]] = {}
    for item in file_items(page):
        (button,) = inside(page, item, lambda e: "data-file-download" in e.attrs)
        key = (str(item.attrs["data-document"]), str(item.attrs["data-format"]))
        found[key] = (button.text.strip(), str(button.attrs["data-file-download"]))
    return found


# --- reading the files the way a user's reader would ----------------------------------------------


def squash(text: str) -> str:
    """NFKC, no bidi or zero-width characters, no whitespace: layout is not content."""
    folded = unicodedata.normalize("NFKC", text)
    kept = "".join(c for c in folded if c not in BIDI_CONTROLS and c not in ZERO_WIDTH)
    return "".join(kept.split())


def text_of(content: bytes, fmt: str) -> str:
    return squash(pdf_text(content) if fmt == "pdf" else docx_text(content))


def assert_text_is_exactly(
    extracted: str, pieces: Sequence[str | tuple[str, ...]], *, known: Sequence[str] = ()
) -> None:
    """Every piece is found, in this order, in the squashed ``extracted`` text, and nothing else is
    there: once the pieces and the ``known`` contact values are taken out, only separators and the
    words "present", "current" and "at" are left. A tuple is a group whose parts may come in any
    order (a role's title, employer and dates; the contact lines)."""
    position, leftovers = 0, []
    for piece in pieces:
        parts = (piece,) if isinstance(piece, str) else piece
        spans = []
        for part in parts:
            squashed = squash(part)
            found = extracted.find(squashed, position)
            assert found >= 0, f"{part!r} is not in the file after position {position}"
            spans.append((found, found + len(squashed)))
        spans.sort()
        for start, end in spans:
            assert start >= position, f"{piece!r} overlaps the text before it"
            leftovers.append(extracted[position:start])
            position = end
    leftovers.append(extracted[position:])
    rest = "".join(leftovers)
    for value in sorted((squash(v) for v in known), key=len, reverse=True):
        rest = rest.replace(value, "")
    assert FILLER.fullmatch(rest), f"text in the file that is not approved content: {rest!r}"


# --- doubles --------------------------------------------------------------------------------------


class Probe:
    """Wraps a renderer: remembers every document it was given, whether a render ran in a thread
    that has an event loop (it must not), and can fail on its Nth call."""

    def __init__(
        self, inner: Any, *, fail_on_call: int | None = None, error: Exception | None = None
    ) -> None:
        self.inner = inner
        self.calls: list[Any] = []
        self.on_loop: list[bool] = []
        self.fail_on_call = fail_on_call
        self.error = error

    @property
    def format(self) -> str:
        return str(self.inner.format)

    @property
    def media_type(self) -> str:
        return str(self.inner.media_type)

    def render(self, document: Any) -> Any:
        self.calls.append(document)
        try:
            asyncio.get_running_loop()
            self.on_loop.append(True)
        except RuntimeError:
            self.on_loop.append(False)
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            assert self.error is not None
            raise self.error
        return self.inner.render(document)


class UnavailablePdf:
    """A PDF renderer that cannot run here (the library is missing)."""

    format = "pdf"
    media_type = "application/pdf"

    def __init__(self) -> None:
        self.calls = 0

    def render(self, document: Any) -> Any:
        self.calls += 1
        raise RendererUnavailable(PDF_UNAVAILABLE)


def real_renderers(clock: Any) -> tuple[Any, Any]:
    pdf = importlib.import_module("app.render.providers.pdf")
    word = importlib.import_module("app.render.providers.word")
    return pdf.PdfRenderer(clock), word.WordRenderer(clock)


class RendererWiring:
    """The app's wiring except for ``document_renderers``."""

    def __init__(self, wiring: Any, renderers: Sequence[Any]) -> None:
        self._wiring = wiring
        self._renderers = tuple(renderers)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wiring, name)

    def document_renderers(self) -> tuple[Any, ...]:
        return self._renderers


# --- the running app ------------------------------------------------------------------------------


class Exporting(LetterDesk):
    """The letter harness plus exports, the Submit step and the freeze."""

    probes: tuple[Probe, Probe] | None = None

    @property
    def data_dir(self) -> Path:
        return self.r.run.data_dir

    @property
    def run_headers(self) -> Any:
        return self.r.run.run

    # --- seeding ------------------------------------------------------------------------------

    def export_ready(
        self,
        job_id: str = "export-job",
        *,
        details: bool = True,
        photo: bool = False,
        letter: bool = True,
        cv_approved: bool = True,
        letter_approved: bool = True,
        **template_changes: Any,
    ) -> Any:
        """An application with a PT template (``template_changes`` override its fields, for
        example ``photo_slot=False`` or ``max_pages=1``), the saved details (and a photo), a
        generated CV and, with ``letter``, a drafted letter; each approved unless said otherwise.
        Nothing is exported."""
        app = self.cv_ready(job_id, template=False)
        if not self.rows("SELECT id FROM cv_templates WHERE country='PT'"):
            self.put_template("PT", **template_changes)
        if details:
            self.save_contact(photo=photo)
        self.generate(app.id, "ok")
        if cv_approved:
            self.service.approve(app.id, self.current_version_id(app.id))
        if letter:
            self.draft(app.id, "ok")
            if letter_approved:
                self.letters.approve(app.id, self.current_letter_id(app.id))
        return app

    def save_contact(self, *, photo: bool = False) -> None:
        desk = DetailsDesk(self.r.run)
        assert desk.save(desk.version()).status_code == 200
        if photo:
            sent = desk.upload(real_png(100, 100), filename="me.png", content_type="image/png")
            assert sent.status_code == 200, sent.text[:300]

    def photo_bytes(self) -> bytes:
        name = DetailsDesk(self.r.run).photo_name()
        assert name is not None, "no photo is saved"
        return (self.data_dir / "assets" / name).read_bytes()

    def add_posting_link(self, job_id: str, url: str = POSTING_URL) -> None:
        self.r.run.execute(
            "INSERT INTO job_sources (id,job_id,url,canonical_url,kind,found_at,created_at) "
            "VALUES (?,?,?,?,'link',?,?)",
            (
                f"source-{job_id}",
                job_id,
                url,
                url,
                "2026-10-20T09:00:00+00:00",
                "2026-10-20T09:00:00+00:00",
            ),
        )

    # --- requests -----------------------------------------------------------------------------

    def export(self, aid: str, values: dict[str, str] | None = None) -> Response:
        return self.post_fields(export_url(aid), values or {})

    def export_ok(self, aid: str) -> Response:
        answer = self.export(aid)
        assert answer.status_code == 200, (answer.status_code, answer.text[:300])
        return answer

    def step5(self, aid: str, **headers: str) -> Response:
        return self.r.client.get(step5_url(aid), headers=self.run_headers.htmx(**headers))

    def page5(self, aid: str) -> Response:
        return self.r.client.get(step5_page_url(aid))

    def download(self, aid: str, export_id: str) -> Response:
        return self.r.client.get(
            download_url(aid, export_id), headers=self.run_headers.with_token()
        )

    def application_row(self, aid: str) -> dict[str, Any]:
        (row,) = self.rows("SELECT * FROM applications WHERE id=?", (aid,))
        return row

    def mark_applied(self, aid: str, *, version: str | None = None, **extra: str) -> Response:
        shown = version or str(self.application_row(aid)["version"])
        return self.post_fields(applied_url(aid), {"version": shown, **extra})

    def move(
        self,
        aid: str,
        status: str,
        how: str = "menu",
        *,
        version: int | None = None,
    ) -> Response:
        """The existing status route, as the board (drag), its menu and the drawer post it."""
        current = version if version is not None else self.application_row(aid)["version"]
        values = {
            "version": str(current),
            "status": status,
            "source": "board" if how == "board" else "menu",
        }
        headers = self.run_headers.action(
            **{
                "HX-Request": "true",
                **({"HX-Target": "application-detail"} if how == "drawer" else {}),
            }
        )
        return self.r.client.post(f"/applications/{aid}/status", data=values, headers=headers)

    def move_ok(self, aid: str, status: str, how: str = "menu") -> Response:
        answer = self.move(aid, status, how)
        assert answer.status_code == 200, (answer.status_code, answer.text[:300])
        return answer

    # --- what is stored -----------------------------------------------------------------------

    def exports(self, aid: str | None = None) -> list[dict[str, Any]]:
        """Every export row with its document ``kind``, the version ``number`` and the
        ``application_id``, oldest first."""
        sql = (
            "SELECT e.*, v.number AS number, d.kind AS kind, d.application_id AS application_id "
            "FROM document_exports e JOIN document_versions v ON v.id=e.document_version_id "
            "JOIN documents d ON d.id=v.document_id"
            + ("" if aid is None else " WHERE d.application_id=?")
            + " ORDER BY e.created_at, e.id"
        )
        return self.rows(sql, () if aid is None else (aid,))

    def export_of(self, aid: str, kind: str, fmt: str, number: int | None = None) -> dict[str, Any]:
        found = [
            row
            for row in self.exports(aid)
            if row["kind"] == kind and row["format"] == fmt and number in (None, row["number"])
        ]
        assert len(found) == 1, f"expected one {kind} {fmt} export, found {len(found)}"
        return found[0]

    def folder(self, aid: str) -> Path:
        return self.data_dir / "documents" / aid

    def folder_files(self, aid: str) -> list[Path]:
        folder = self.folder(aid)
        if not folder.exists():
            return []
        return sorted(folder.rglob("*"))

    def file_bytes(self, row: dict[str, Any]) -> bytes:
        return (self.folder(row["application_id"]) / row["stored_name"]).read_bytes()

    def all_files_under_data(self) -> set[str]:
        found: set[str] = set()
        for folder, _names, files in os.walk(self.data_dir):
            for name in files:
                found.add(str(Path(folder, name).relative_to(self.data_dir)))
        return found

    def folder_state(self, aid: str) -> dict[str, tuple[str, int, int]]:
        """Per file: (sha256, mode, mtime in ns), to prove a file was not touched."""
        state: dict[str, tuple[str, int, int]] = {}
        for path in self.folder_files(aid):
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                state[path.name] = (digest, stat.S_IMODE(info.st_mode), info.st_mtime_ns)
        return state

    def submissions(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM application_submissions ORDER BY application_id")

    def status_of_version(self, version_id: str) -> str:
        return str(self.version_row(version_id)["status"])

    def history_sources(self, aid: str) -> list[str]:
        return [
            row["source"]
            for row in self.rows(
                "SELECT source FROM status_history WHERE application_id=? ORDER BY changed_at, id",
                (aid,),
            )
        ]

    def all_tables(self) -> dict[str, list[dict[str, Any]]]:
        names = [
            row["name"]
            for row in self.rows(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        return {name: self.rows(f"SELECT * FROM {name} ORDER BY 1") for name in names}  # noqa: S608

    # --- the expected words of an approved document --------------------------------------------

    def cv_pieces(
        self, aid: str, version_id: str | None = None
    ) -> tuple[list[str | tuple[str, ...]], list[str]]:
        """(pieces in reading order, known contact values) of a CV version as step 3 shows it."""
        view = self.service.view(aid, version_id)
        assert view.contact.name, "the details have no name"
        known = [view.contact.name, *view.contact.lines]
        pieces: list[str | tuple[str, ...]] = [tuple(known)]
        for section in view.sections:
            pieces.append(section.heading)
            for block in section.blocks:
                header = block.header
                if header is not None:
                    parts = (header.title, header.employer, header.start_text, header.end_text)
                    pieces.append(tuple(part for part in parts if part))
                pieces.extend(unit.text for unit in block.units)
        return pieces, known

    def letter_pieces(
        self, aid: str, version_id: str | None = None
    ) -> tuple[list[str | tuple[str, ...]], list[str]]:
        view = self.letters.view(aid, version_id)
        frame = view.frame
        pieces: list[str | tuple[str, ...]] = [
            frame.greeting,
            frame.opening,
            *(unit.text for unit in view.units),
            frame.closing,
            frame.name,
        ]
        details = DetailsDesk(self.r.run).stored() or {}
        known = [
            str(v)
            for v in (
                details.get("email"),
                details.get("phone"),
                details.get("location"),
                *details.get("links", ()),
            )
            if v
        ]
        return pieces, [frame.name, *known]


@contextmanager
def exporting_app(
    tmp_path: Path,
    *,
    renderers: Callable[[FrozenClock], Sequence[Any]] | None = None,
    seed: bool = True,
) -> Iterator[Exporting]:
    """The running app on ``tmp_path`` (a finished setup, the fake provider behind the spy, the real
    task runner). ``renderers`` builds the app's renderers from the app's clock (default: the real
    PDF and Word renderers wiring builds)."""
    if seed:
        finished_guide(tmp_path)
    hold = Hold(DEADLINE_S)
    spy = SpyProvider(hold)
    clock = FrozenClock(START)
    configured = make_wiring(None)
    configured.clock = clock
    wiring: Any = SpyWiring(configured, spy)
    if renderers is not None:
        wiring = RendererWiring(wiring, renderers(clock))
    with patch("app.main.UuidIdFactory", SequentialIdFactory):
        app = create_app(make_config(tmp_path), clock=clock, wiring=wiring)
    try:
        with TestClient(
            app, base_url=BASE_URL, follow_redirects=False, raise_server_exceptions=False
        ) as client:
            running = Running(app=app, client=client, hits=Counter())
            yield Exporting(Refining(Importing(running, spy, hold, clock, tmp_path / "data")))
    finally:
        hold.open()


def probed(clock: Any) -> tuple[Probe, Probe]:
    pdf, word = real_renderers(clock)
    return Probe(pdf), Probe(word)


@contextmanager
def probed_app(tmp_path: Path, **options: Any) -> Iterator[Exporting]:
    """The app with both real renderers wrapped in a ``Probe`` (``desk.probes``: pdf, word)."""
    made: list[tuple[Probe, Probe]] = []

    def build(clock: Any) -> tuple[Probe, Probe]:
        made.append(probed(clock))
        return made[0]

    with exporting_app(tmp_path, renderers=build, **options) as desk:
        desk.probes = made[0]
        yield desk
