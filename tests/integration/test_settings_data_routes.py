"""M9.1 part D1: Settings > Data and privacy, the routes (AC-M9.3; ADR-131 items 2-4; spec 2.9
rules 2, 3, 5, 9, 12, 13 and 3.8; TD-033).

Assumed interface, markup hooks and helpers: the docstring of ``tests/data_pages.py``. The routes
live in ``app/api/settings_data.py``; ``app.state.data_portability`` is wired by this part, and
``app.state.data_erasure`` already exists. Real SQLite in a temp folder, the app through
``TestClient`` (or a raw ASGI driver for the streamed upload), no network. Every value is
fictional. The new names are imported inside the tests, so each one fails on its own while they
are missing.
"""

import asyncio
import json
import logging
import tracemalloc
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute

from app.api.security import PAGE_TAG
from app.modules.secrets.providers.keyring_store import SERVICE_NAME, KeyringSecretStore
from app.onboarding.progress import SetupProgress
from app.services.setup_state import SetupStateService
from tests.asgi_stream import app_lifespan
from tests.board_pages import by_id, elements
from tests.data_pages import (
    DELETE,
    ERASED_EVENT,
    ERASED_NOTICE,
    EXPORT,
    EXPORT_NAME,
    FACT_TEXT,
    FILE_NOT_SAVED,
    FILES,
    IMPORT,
    IMPORT_BLOCKED_NOTE,
    IMPORT_BUSY,
    INVALID,
    LOCATION,
    MARKER,
    MIB,
    NOTHING_IMPORTED,
    PAGE,
    UPLOAD_TOO_SLOW,
    add_fact,
    announced,
    download_button,
    drive,
    error_tied_to,
    focus_targets,
    folder_tree,
    generated_name,
    get_token,
    import_is_blocked,
    inside,
    label_for,
    make_app,
    multipart_stream,
    one,
    page_text,
    plant,
    post,
    prepare,
    running,
    source_export,
    state_of,
    upload,
    upload_folders,
    workspace_state,
)
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import MemoryKeyring
from tests.integration.test_data_erasure import SECRET_VALUES, planted_keys
from tests.integration.test_setup_redirect import ORIGIN, START
from tests.leak_scan import RawLogRecords
from tests.upload_bodies import BOUNDARY, CONTENT_TYPE, FieldPart, FilePart, multipart_body

pytestmark = pytest.mark.integration

ROUTE_STATUS = {  # the refusals of an import and the status each one answers (CHOICE)
    "bad_json": 422,
    "not_an_export": 422,
    "unknown_top_level": 422,
    "unknown_table": 422,
    "sha_mismatch": 422,
    "unsafe_file_name": 422,
    "newer_schema": 422,
    "non_empty_workspace": 409,
    "oversize": 413,
}
PRIVATE_NAME = "kestrel-private-name-3377.json"
DETAIL = "fictional-detail-token-8801"


# --- the page, the fragments and the token ---


def test_data_routes_token_origin_and_get_writes_nothing(tmp_path: Path) -> None:
    """Only the page loads without the token; every fragment and POST needs it, every POST also
    the origin; a GET (and a refused request) writes nothing (spec 2.9 rules 2, 3, 12)."""
    with running(tmp_path, empty=False, files=True) as run:
        data = tmp_path / "data"
        add_fact(run)
        before = state_of(run, data)

        # The page: token-free, read-only; htmx needs the token and then gets the content alone.
        for method in ("GET", "HEAD"):
            assert run.client.request(method, PAGE).status_code == 200, method
        assert run.client.get(PAGE, headers={"HX-Request": "true"}).status_code == 403
        full = run.client.get(PAGE)
        assert "<html" in full.text and one(full.text, "data-export").tag == "section"
        part = get_token(run, PAGE, htmx=True)
        assert part.status_code == 200 and "<html" not in part.text
        assert one(part.text, "data-delete").tag == "section"

        # Protected GET fragments.
        for path in (LOCATION, DELETE):
            assert run.client.get(path).status_code == 403, path
            assert run.client.get(path, headers={"HX-Request": "true"}).status_code == 403, path
            assert get_token(run, path).status_code == 200, path
            assert get_token(run, path, htmx=True).status_code == 200, path
        name_path = f"{PAGE}/exports/jobpipeline-export-20260103T090000000000Z.json"
        assert run.client.get(name_path).status_code == 403
        assert run.client.get(name_path, headers={"HX-Request": "true"}).status_code == 403

        # Every POST: no token, then a token without or with a foreign origin, is refused.
        uploads = (
            (EXPORT, {"data": {}}),
            (DELETE, {"data": {"confirm": "delete"}}),
            (IMPORT, {"upload": True}),
        )
        for path, kwargs in uploads:
            for token, origin in (
                (False, True),
                (True, False),
                (True, "http://evil.example"),
                (False, False),
            ):
                if kwargs.get("upload"):
                    answer = upload(run, b"{}", token=token, origin=origin)
                else:
                    answer = post(run, path, kwargs["data"], token=token, origin=origin)
                assert answer.status_code == 403, (path, token, origin)
        assert state_of(run, data) == before, "a refused request changes nothing"

        # Only the verbs each route is for.
        token = run.with_token(origin=ORIGIN)
        for path in (EXPORT, IMPORT):
            assert run.client.get(path, headers=token).status_code == 405, path
        for path in (PAGE, EXPORT, IMPORT, DELETE, LOCATION):
            for method in ("PUT", "PATCH", "DELETE"):
                answer = run.client.request(method, path, headers=token)
                assert answer.status_code in (404, 405), (method, path)
        assert run.client.post(PAGE, headers=token).status_code in (404, 405)
        assert run.client.post(LOCATION, headers=token).status_code in (404, 405)
        assert state_of(run, data) == before, "a GET or a refused verb changes nothing"


def test_data_page_never_contains_the_folder_path_but_the_protected_fragment_does(
    tmp_path: Path,
) -> None:
    """Spec 2.9 rule 13: a token-free page shows stored data to any local process, so the path of
    the data folder is only in a fragment that needs the token."""
    root = tmp_path / "fictional-harbour-root-5521"
    with running(root, empty=False) as run:
        data = root / "data"
        needles = {str(data), str(data.resolve()), root.name}
        answers = {
            "page": run.client.get(PAGE),
            "page (htmx)": get_token(run, PAGE, htmx=True),
            "page (token only)": get_token(run, PAGE),
            "delete dialog": get_token(run, DELETE),
        }
        for label, answer in answers.items():
            assert answer.status_code == 200, label
            text = answer.text
            for needle in needles:
                assert needle not in text, f"{label} holds the folder path"
            for name, value in answer.headers.items():
                assert not any(n in value for n in needles), f"{label} header {name}"

        # The page asks for the fragment when it loads, and says where the path will be.
        loader = one(answers["page"].text, "data-location")
        assert loader.attrs.get("hx-get") == LOCATION
        assert loader.attrs.get("hx-trigger") == "load"

        fragment = get_token(run, LOCATION, htmx=True)
        assert fragment.status_code == 200
        shown = page_text(fragment.text)
        assert str(data) in shown or str(data.resolve()) in shown
        assert one(fragment.text, "data-folder").tag == "code"
        refused = run.client.get(LOCATION)
        assert refused.status_code == 403
        assert not any(n in refused.text for n in needles)


def test_data_page_sections_and_dialog_markup(tmp_path: Path) -> None:
    """What the page offers, with the accessibility hooks the htmx-ui skill asks for."""
    with running(tmp_path, empty=False) as run:
        page = run.client.get(PAGE).text
        assert one(page, "page-heading").text == "Data and privacy"
        everything = elements(page)
        # M9.2: the "Back to Settings" link is replaced by the side list of Settings areas,
        # whose first link is the way back (Providers and AI, /settings); the page's one <h1>
        # leads the main content.
        side_list = [
            e
            for e in everything
            if e.tag == "nav" and e.attrs.get("aria-label") == "Settings areas"
        ]
        assert len(side_list) == 1
        back = [
            e for e in inside(page, side_list[0]) if e.tag == "a" and e.text == "Providers and AI"
        ]
        assert len(back) == 1 and back[0].attrs.get("href") == "/settings", "a way back"
        assert not [e for e in everything if e.tag == "a" and e.text == "Back to Settings"]

        export = one(page, "data-export")
        assert export.tag == "section"
        assert one(page, "data-export-heading").text == "Export"
        form = one(page, "data-export-form")
        assert form.attrs.get("action") == EXPORT and form.attrs.get("method", "").lower() == "post"
        boxes = [e for e in inside(page, form) if e.attrs.get("name") == "snapshots"]
        assert len(boxes) == 1
        box = boxes[0]
        assert box.attrs.get("type") == "checkbox" and box.attrs.get("value") == "1"
        assert "checked" not in box.attrs, "usage snapshots are left out unless ticked (TD-012)"
        assert label_for(page, box) == "Include provider usage snapshots"
        hint = one(page, str(box.attrs.get("aria-describedby")))
        assert hint.within(form) and hint.text == (
            "Adds the saved usage counts for your AI and search providers. "
            "It holds no keys or text."
        )
        buttons = [e for e in inside(page, form) if e.tag == "button"]
        assert [b.text for b in buttons] == ["Export"]

        importing = one(page, "data-import")
        assert importing.tag == "section" and one(page, "data-import-heading").text == "Import"
        import_form = one(page, "data-import-form")
        assert import_form.attrs.get("action") == IMPORT
        assert import_form.attrs.get("method", "").lower() == "post"
        assert import_form.attrs.get("enctype") == "multipart/form-data"
        # This workspace has content, so Import is blocked and offers no file to choose; the
        # file field itself is checked on an empty workspace below.
        assert by_id(page, "data-import-file") is None
        assert one(page, "data-import-submit").text == "Import"

        deleting = one(page, "data-delete")
        assert deleting.tag == "section" and one(page, "data-delete-heading").text == (
            "Delete all data"
        )
        opener = one(page, "data-delete-open")
        assert opener.tag == "button" and opener.text.startswith("Delete all data")
        assert not [e for e in inside(page, deleting) if e.tag == "dialog"], (
            "the dialog is a fragment opened by the button, not part of the page"
        )

        # The dialog (htmx-ui "Modal question"): focus trap and return focus come from app.js.
        fragment = get_token(run, DELETE, htmx=True).text
        dialog = one(fragment, "data-delete-dialog")
        assert dialog.tag == "dialog" and "data-modal" in dialog.attrs
        assert dialog.attrs.get("role") == "dialog" and dialog.attrs.get("aria-modal") == "true"
        assert dialog.attrs.get("data-return-focus") == "#data-delete-open"
        title = one(fragment, str(dialog.attrs.get("aria-labelledby")))
        about = one(fragment, str(dialog.attrs.get("aria-describedby")))
        assert title.within(dialog) and about.within(dialog)
        form = one(fragment, "data-delete-form")
        assert form.attrs.get("action") == DELETE and form.attrs.get("method", "").lower() == "post"
        typed = [e for e in inside(fragment, form) if e.attrs.get("name") == "confirm"]
        assert len(typed) == 1 and typed[0].tag == "input"
        assert label_for(fragment, typed[0]) == "Type delete to confirm"
        label = next(e for e in elements(fragment) if e.tag == "label")
        assert [e.text for e in inside(fragment, label) if e.tag == "code"] == ["delete"]
        hint = one(fragment, "data-delete-hint")
        assert hint.text == (
            "Type it in lowercase. The Delete all data button stays off until then."
        )
        assert "data-delete-hint" in str(typed[0].attrs.get("aria-describedby")).split()
        bullets = [e.text for e in inside(fragment, dialog) if e.tag == "li"]
        assert (
            "Keys the app saved in your computer's keychain are removed too. They are shared by "
            "every workspace on this computer, unless the app's setup file points at another "
            "data folder."
        ) in bullets
        assert not [b for b in bullets if b.startswith("Keychain items")]
        closers = [e for e in inside(fragment, dialog) if "data-modal-close" in e.attrs]
        assert closers and "autofocus" in closers[0].attrs, "the safe choice has focus first"
        assert len([e for e in elements_with_autofocus(fragment)]) == 1
        confirm = one(fragment, "data-delete-submit")
        assert confirm.attrs.get("aria-disabled") == "true" or "disabled" in confirm.attrs, (
            "Delete stays blocked until the word is typed"
        )

        # The wording: export first, and the key set in an environment variable.
        said = page_text(page) + " " + page_text(fragment)
        assert "export" in said.lower()
        assert "environment variable" in said.lower() or "environment" in said.lower()
        assert "can't be removed" in said or "cannot be removed" in said, said


def elements_with_autofocus(markup: str) -> list[Any]:
    return [e for e in elements(markup) if "autofocus" in e.attrs]


def confirm_field(markup: str) -> Any:
    fields = [
        e
        for e in inside(markup, one(markup, "data-delete-form"))
        if e.attrs.get("name") == "confirm"
    ]
    assert len(fields) == 1
    return fields[0]


def test_data_page_import_disabled_unless_workspace_empty(tmp_path: Path) -> None:
    """Import is a blocked action (aria-disabled, explained) on a workspace with content, and
    open on one with none; the page follows the workspace without a restart."""
    with running(tmp_path / "full", empty=False) as run:
        page = run.client.get(PAGE).text
        assert import_is_blocked(page) is True
        assert one(page, "data-import-note").text == IMPORT_BLOCKED_NOTE
        assert "Delete all data" not in one(page, "data-import-note").text, (
            "no pointer to a restore that setup does not offer yet"
        )
        assert "Use Import on a new workspace" not in page_text(page)
    with running(tmp_path / "files", empty=True) as run:
        plant(tmp_path / "files" / "data", {"imports/fictional-cv.txt": b"fictional"})
        assert import_is_blocked(run.client.get(PAGE).text) is True, "a file is content too"
    with running(tmp_path / "empty", empty=True) as run:
        page = run.client.get(PAGE).text
        assert import_is_blocked(page) is False
        field = one(page, "data-import-file")
        assert field.attrs.get("type") == "file" and field.attrs.get("name") == "file"
        assert label_for(page, field) == "Export file (.json)"
        add_fact(run)
        assert import_is_blocked(get_token(run, PAGE, htmx=True).text) is True, (
            "content written after the first read blocks it"
        )


# --- export and download ---


def test_export_route_writes_private_file_and_download_needs_token(tmp_path: Path) -> None:
    with running(tmp_path, empty=False, files=True) as run:
        data = tmp_path / "data"
        add_fact(run)
        planted = sorted(p.name for p in (data / "exports").iterdir())

        # Only the checkbox's own value; nothing else is a field.
        for bad in (
            {"snapshots": "2"},
            {"snapshots": ""},
            {"other": "1"},
            {"snapshots": ["1", "1"]},
        ):
            answer = post(run, EXPORT, bad)
            assert answer.status_code == 400 and answer.text == INVALID, bad
        assert sorted(p.name for p in (data / "exports").iterdir()) == planted, "nothing exported"

        answer = post(run, EXPORT, {})
        assert answer.status_code == 200
        block = one(answer.text, "data-export")
        assert block.tag == "section"
        name = generated_name(answer.text)
        assert EXPORT_NAME.fullmatch(name)
        button = download_button(answer.text)
        assert button is not None and button.tag == "button"
        assert button.attrs["data-file-download"] == f"{PAGE}/exports/{name}"
        assert "Download" in button.text
        assert "hidden" in button.attrs, "revealed by app.js, which supplies the token header"
        assert one(answer.text, "data-export-done").text == (
            f"Export ready: {name}. It is saved in the app's exports folder. Download a copy "
            "and keep it somewhere private. Delete all data removes this one too."
        )
        strong = [e for e in elements(answer.text) if e.tag == "strong"]
        assert [e.text for e in strong] == [name], "only the file name is strong"
        assert all(e.within(one(answer.text, "data-export-done")) for e in strong)
        assert len(focus_targets(answer.text)) == 1
        assert announced(answer) == f"Export ready. {name}"

        path = data / "exports" / name
        assert path.is_file() and (path.stat().st_mode & 0o777) == 0o600
        assert ((data / "exports").stat().st_mode & 0o777) == 0o700
        payload = json.loads(path.read_bytes())
        assert payload["format"] == 1 and isinstance(payload["schema_version"], int)
        assert FACT_TEXT.encode() in path.read_bytes(), "the export holds the fact"
        assert {item["name"] for item in payload["files"]} == set(FILES)
        assert "provider_usage_snapshots" in payload["excluded"]
        assert "provider_usage_snapshots" not in payload["tables"]
        # The export holds neither the backups, logs or earlier exports nor the keys.
        assert not [
            n
            for n in (i["name"] for i in payload["files"])
            if n.startswith(("logs/", "backups/", "exports/"))
        ]

        again = post(run, EXPORT, {"snapshots": "1"})
        assert again.status_code == 200
        second = generated_name(again.text)
        assert second != name and EXPORT_NAME.fullmatch(second)
        opted = json.loads((data / "exports" / second).read_bytes())
        assert "provider_usage_snapshots" in opted["tables"], "ticked: the snapshots are in"

        # Download: token needed, the bytes as stored, fixed type and name, no caching.
        url = f"{PAGE}/exports/{name}"
        assert run.client.get(url).status_code == 403
        assert run.client.get(url, headers={"HX-Request": "true"}).status_code == 403
        before = state_of(run, data)
        answer = get_token(run, url)
        assert answer.status_code == 200
        assert answer.content == path.read_bytes()
        assert answer.headers["Content-Type"] == "application/json"
        assert answer.headers["Content-Disposition"] == f'attachment; filename="{name}"'
        assert answer.headers["Cache-Control"] == "no-store"
        assert state_of(run, data) == before, "a download writes nothing"
        token = run.with_token(origin=ORIGIN)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert run.client.request(method, url, headers=token).status_code in (404, 405)


def test_export_download_rejects_names_outside_the_generated_pattern(tmp_path: Path) -> None:
    secret = b"a secret outside the data folder, never to be served"
    outside = tmp_path / "outside-secret.json"
    outside.write_bytes(secret)
    # Not the frozen clock's own stamp (START, ...000000Z): the control export at the end would
    # then collide with the planted link and be refused, as the service must (links refused).
    stamp = "jobpipeline-export-20260103T090000000002Z"
    with running(tmp_path / "app", empty=False) as run:
        data = tmp_path / "app" / "data"
        exports = data / "exports"
        exports.mkdir(mode=0o700, exist_ok=True)
        for name in (
            "notes.txt",
            "secret.json",
            "jobpipeline-export-bad.json",
            f"{stamp}.json.bak",
        ):
            (exports / name).write_bytes(secret)
        (exports / f"{stamp}.json").symlink_to(outside)
        (exports / f"{stamp}-2.json").hardlink_to(outside)
        before = state_of(run, data)

        bodies: set[str] = set()
        names = [
            "notes.txt",
            "secret.json",
            "app.db",
            "jobpipeline-export-bad.json",
            f"{stamp}.json.bak",
            f"{stamp}.json",  # a link
            f"{stamp}-2.json",  # a second name of a file outside
            f"{stamp}-0.json",  # the counter starts at 1
            f"{stamp.upper()}.JSON",
            "jobpipeline-export-20260103T090000000001Z.json",  # shaped right, not there
            "x" * 400,
        ]
        for name in names:
            answer = get_token(run, f"{PAGE}/exports/{name}")
            assert answer.status_code == 404, name
            assert secret not in answer.content and b"SQLite format" not in answer.content
            assert name not in answer.text or len(name) < 3
            assert answer.headers["Cache-Control"] == "no-store"
            bodies.add(answer.text)
        assert len(bodies) == 1, f"every refusal says the same words, got {len(bodies)}"
        assert not bodies & {""}, "the 404 explains itself in plain text"
        for tricky in ("%2E%2E%2Fapp.db", "..%2F..%2Fapp.db", "%2Fetc%2Fpasswd", "a%5Cb.json"):
            answer = get_token(run, f"{PAGE}/exports/{tricky}")
            assert answer.status_code == 404, tricky
            assert b"SQLite format" not in answer.content and secret not in answer.content
        assert state_of(run, data) == before

        # Control: the generated name of a real export is served.
        name = generated_name(post(run, EXPORT, {}).text)
        assert get_token(run, f"{PAGE}/exports/{name}").status_code == 200


# --- the text of an export never leaves the file ---


def test_export_text_never_in_logs_task_rows_or_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Spec 2.10: the stored text a person exports and restores (here a fact) reaches the file and
    the answers' own file name only: not a log record (raw, before redaction), a task or usage
    row, a published task event, a response header or a path."""
    caplog.set_level(logging.DEBUG)
    records = RawLogRecords()
    records.install(monkeypatch)
    events: list[str] = []
    from app.api.events import TaskEvents

    original = TaskEvents.publish

    def record(self: Any, change: Any) -> None:
        events.append(repr(change))
        original(self, change)

    monkeypatch.setattr(TaskEvents, "publish", record)

    surfaces: list[tuple[str, str]] = []

    def scan(label: str, run: Any, *answers: Any) -> None:
        for number, answer in enumerate(answers):
            surfaces.append((f"{label} answer {number} body", answer.text))
            surfaces.append((f"{label} answer {number} request", str(answer.request.url)))
            for name, value in answer.headers.items():
                surfaces.append((f"{label} answer {number} header {name}", f"{name}: {value}"))
        with run.app.state.workspace.db.read() as conn:
            for table in ("tasks", "usage_records"):
                surfaces.append(
                    (
                        f"{label} {table} rows",
                        repr(conn.execute(f"SELECT * FROM {table}").fetchall()),  # noqa: S608
                    )
                )

    with running(tmp_path / "a", empty=False, files=True) as run_a:
        add_fact(run_a)
        exported = post(run_a, EXPORT, {})
        name = generated_name(exported.text)
        downloaded = get_token(run_a, f"{PAGE}/exports/{name}")
        assert MARKER.encode() in downloaded.content, "control: the file holds the text"
        scan("export", run_a, exported)
        surfaces.append(("download request", str(downloaded.request.url)))
        for key, value in downloaded.headers.items():
            surfaces.append((f"download header {key}", f"{key}: {value}"))
        raw = downloaded.content

    with running(tmp_path / "b", empty=True) as run_b:
        restored = upload(run_b, raw, filename=PRIVATE_NAME)
        assert restored.status_code == 200, restored.text
        with run_b.app.state.workspace.db.read() as conn:
            stored = repr(conn.execute("SELECT * FROM fact_versions").fetchall())
        assert MARKER in stored, "control: the restore brought the fact back"
        scan("import", run_b, restored)

    assert records.entries, "control: the scan saw log records"
    surfaces.extend((where, text) for where, text in records.entries)
    surfaces.extend((f"task event {n}", text) for n, text in enumerate(events))
    leaks = [where for where, text in surfaces if MARKER in text or PRIVATE_NAME in text]
    assert not leaks, f"the exported text or the uploaded file's name reached: {leaks}"


# --- import: the stream, the cap and the timeout ---


def test_import_upload_streamed_with_read_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TD-033, spec 2.9 rule 5. The cap (200 MiB) and the read timeout are the route's own; the
    body is counted as it arrives and written to disk, so a body over the cap is refused 413
    after about the cap, never read whole; a stream that stalls is answered, not waited for."""
    from app.api import settings_data

    assert settings_data.MAX_IMPORT_BYTES == 200 * MIB
    assert 5 <= settings_data.READ_TIMEOUT_S <= 120

    monkeypatch.setattr(settings_data, "MAX_IMPORT_BYTES", MIB)
    monkeypatch.setattr(settings_data, "READ_TIMEOUT_S", 5.0)
    root = tmp_path / "ws"
    data = prepare(root, empty=True)
    app = make_app(root)
    chunk = 64 * 1024
    valid = source_export(tmp_path / "source", rows=False)
    assert len(valid) < MIB

    async def endless_zeros(total: int) -> Any:
        async def chunks() -> Any:
            for _ in range(total // chunk):
                yield b"0" * chunk

        return chunks

    async def stalls() -> Any:
        async def chunks() -> Any:
            yield b'{"format": 1, "files": ['
            await asyncio.Event().wait()  # the client goes silent
            yield b""

        return chunks

    async def small_pieces() -> Any:
        async def chunks() -> Any:
            for start in range(0, len(valid), 977):
                yield valid[start : start + 977]

        return chunks

    def on_disk(asked: int) -> list[str]:
        return upload_folders(data) if asked == 8 else []

    async def scenario() -> dict[str, Any]:
        found: dict[str, Any] = {}
        async with app_lifespan(app):
            token = app.state.launch_token
            empty_tree = folder_tree(data)
            state = workspace_state(app)

            # 1. Twelve MiB offered against a 1 MiB cap.
            tracemalloc.start()
            try:
                over = await drive(
                    app,
                    IMPORT,
                    multipart_stream(await endless_zeros(12 * MIB)),
                    token=token,
                    midway=on_disk,
                )
                found["peak"] = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            found["over"] = over
            found["after_over"] = (folder_tree(data) == empty_tree, workspace_state(app) == state)

            # 2. A stream that stalls, with a short timeout.
            monkeypatch.setattr(settings_data, "READ_TIMEOUT_S", 0.3)
            found["stalled"] = await drive(
                app, IMPORT, multipart_stream(await stalls()), token=token, deadline=10.0
            )
            found["after_stalled"] = (
                folder_tree(data) == empty_tree,
                workspace_state(app) == state,
            )

            # 3. A valid export in odd pieces is parsed and restored.
            monkeypatch.setattr(settings_data, "READ_TIMEOUT_S", 5.0)
            found["valid"] = await drive(
                app, IMPORT, multipart_stream(await small_pieces()), token=token
            )
            found["restored"] = {
                name: (data / name).read_bytes() for name in FILES if (data / name).is_file()
            }
        return found

    found = asyncio.run(scenario())

    over = found["over"]
    assert over.status == 413, over.body[:200]
    assert over.delivered < 6 * MIB, "the app stopped reading long before the end of the body"
    assert over.delivered <= MIB + 3 * 64 * 1024 + chunk, (
        f"the app read {over.delivered} bytes of an upload capped at {MIB}"
    )
    assert any(folders for folders in over.midway), "the upload is written under .restore-upload-*"
    assert found["peak"] < 6 * MIB, f"peak memory {found['peak']} while streaming: not buffered"
    error_tied_to(over.body.decode(), "data-import-file", "data-import-error")
    assert found["after_over"] == (True, True), "an oversize upload leaves nothing behind"
    assert upload_folders(data) == []

    stalled = found["stalled"]
    assert stalled.status in (408, 422), stalled.status
    error_tied_to(stalled.body.decode(), "data-import-file", "data-import-error")
    assert found["after_stalled"] == (True, True), "a stalled upload leaves nothing behind"

    valid_answer = found["valid"]
    assert valid_answer.status == 200, valid_answer.body[:300]
    assert found["restored"] == {name: FILES[name] for name in FILES}
    assert upload_folders(data) == [], "the upload is deleted after a success too"


# --- import: refusals and the round trip ---


@pytest.mark.parametrize("case", list(ROUTE_STATUS))
def test_import_route_refusals_leave_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A refused import answers a fixed message (tied to the file field while it is shown), echoes
    neither the file's contents nor its name, and leaves the data folder and the database as
    they were."""
    if case == "non_empty_workspace":
        data_bytes = source_export(tmp_path / "source", rows=True)
    elif case == "oversize":
        data_bytes = source_export(tmp_path / "source", rows=True)
        from app.api import settings_data

        monkeypatch.setattr(settings_data, "MAX_IMPORT_BYTES", len(data_bytes) - 1)
    else:
        data_bytes = source_export(tmp_path / "source", rows=True, change=case)
    with running(tmp_path / "target", empty=case != "non_empty_workspace", files=False) as run:
        data = tmp_path / "target" / "data"
        if case == "non_empty_workspace":
            plant(data, {"imports/fictional-cv.txt": b"fictional existing file"})
        before = state_of(run, data)

        answer = upload(run, data_bytes, filename=PRIVATE_NAME)

        assert answer.status_code == ROUTE_STATUS[case], (case, answer.text[:300])
        if case == "non_empty_workspace":
            # The note says why, once; the answer adds only that nothing happened. No file field
            # is shown to be filled in again.
            error = one(answer.text, "data-import-error")
            assert error.attrs.get("role") == "alert" and error.text == NOTHING_IMPORTED
            assert one(answer.text, "data-import-note").text == IMPORT_BLOCKED_NOTE
            assert import_is_blocked(answer.text) is True
            message = error.text
            assert page_text(answer.text).count("already has some") == 1
        else:
            message = error_tied_to(answer.text, "data-import-file", "data-import-error")
        assert len(focus_targets(answer.text)) == 1
        text = answer.text + " ".join(f"{k}: {v}" for k, v in answer.headers.items())
        assert MARKER not in text and PRIVATE_NAME not in text, "nothing of the upload is echoed"
        assert "outside.txt" not in text and "unexpected" not in message
        assert str(data) not in text
        assert state_of(run, data) == before, "the workspace is untouched"
        assert upload_folders(data) == [], "the upload was deleted"
        # The next, valid, upload still works where the workspace allows it.
        assert import_is_blocked(run.client.get(PAGE).text) is (case == "non_empty_workspace")


def test_import_route_round_trip_restores_rows_and_files(tmp_path: Path) -> None:
    with running(tmp_path / "a", empty=False, files=True) as run_a:
        add_fact(run_a)
        plant(tmp_path / "a" / "data", FILES)
        name = generated_name(post(run_a, EXPORT, {}).text)
        raw = get_token(run_a, f"{PAGE}/exports/{name}").content
        source = workspace_state(run_a.app)

    with running(tmp_path / "b", empty=True) as run_b:
        data = tmp_path / "b" / "data"
        assert import_is_blocked(run_b.client.get(PAGE).text) is False

        answer = upload(run_b, raw)

        assert answer.status_code == 200, answer.text[:300]
        done = one(answer.text, "data-import-done")
        assert done.text.startswith("Restored")
        links = [e for e in inside(answer.text, one(answer.text, "data-import")) if e.tag == "a"]
        assert "/" in [e.attrs.get("href") for e in links], "a link to start again"
        assert len(focus_targets(answer.text)) == 1
        assert "Restored" in announced(answer)
        for relative, content in FILES.items():
            assert (data / relative).read_bytes() == content, relative
        assert upload_folders(data) == []
        assert not (data / "exports").exists() or not list((data / "exports").iterdir())
        restored = workspace_state(run_b.app)
        assert restored == source, "every row is back"
        assert FACT_TEXT in json.dumps(restored["fact_versions"])
        assert import_is_blocked(run_b.client.get(PAGE).text) is True, "now it has content"
        assert run_b.client.get("/").status_code == 200

        # A second restore into the same workspace is refused (no merging).
        second = upload(run_b, raw)
        assert second.status_code == 409
        assert workspace_state(run_b.app) == restored


def refusal_bodies() -> dict[str, tuple[bytes, str | None]]:
    """Requests that are not one export file in a multipart form: (body, content type)."""
    good = multipart_body(FilePart("export.json", b"{}"))
    return {
        "json_body": (b"{}", "application/json"),
        "urlencoded_form": (b"file=%7B%7D", "application/x-www-form-urlencoded"),
        "plain_text": (b"{}", "text/plain"),
        "no_content_type": (b"{}", None),
        "multipart_without_boundary": (good, "multipart/form-data"),
        "missing_file_field": (multipart_body(FieldPart("{}", name="other")), CONTENT_TYPE),
        "wrong_field_name": (
            multipart_body(FilePart("export.json", b"{}", name="doc")),
            CONTENT_TYPE,
        ),
        "two_file_parts": (
            multipart_body(FilePart("a.json", b"{}"), FilePart("b.json", b"{}")),
            CONTENT_TYPE,
        ),
        "empty_file": (multipart_body(FilePart("export.json", b"")), CONTENT_TYPE),
        "cut_short": (good[:-20], CONTENT_TYPE),
        "no_parts": (f"--{BOUNDARY}--\r\n".encode(), CONTENT_TYPE),
        # A part header is held in memory while it is read: past 8 KiB it is refused (TD-033).
        "oversized_header_value": (padded_header_body(f"X-Pad: {'a' * 20_000}"), CONTENT_TYPE),
        "oversized_header_name": (padded_header_body(f"X-{'a' * 20_000}: x"), CONTENT_TYPE),
    }


def padded_header_body(extra_header: str) -> bytes:
    head = (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="export.json"\r\n'
        f"{extra_header}\r\n\r\n"
    )
    return head.encode() + b"{}" + f"\r\n--{BOUNDARY}--\r\n".encode()


@pytest.mark.parametrize("case", list(refusal_bodies()))
def test_import_route_refuses_what_is_not_one_export_file(tmp_path: Path, case: str) -> None:
    """A request that is not a multipart form with exactly one non-empty ``file`` part is
    answered 422 with the one fixed 'choose a file' message, tied to the file field; nothing is
    written, kept or echoed."""
    body, content_type = refusal_bodies()[case]
    with running(tmp_path, empty=True) as run:
        data = tmp_path / "data"
        before = state_of(run, data)

        answer = post(run, IMPORT, content=body, content_type=content_type)

        assert answer.status_code == 422, (case, answer.status_code, answer.text[:200])
        message = error_tied_to(answer.text, "data-import-file", "data-import-error")
        assert message == "Choose one export file to import. Nothing was imported."
        assert len(focus_targets(answer.text)) == 1
        assert import_is_blocked(answer.text) is False
        assert state_of(run, data) == before and upload_folders(data) == []
        assert import_is_blocked(run.client.get(PAGE).text) is False, "the workspace is still open"


def test_import_declared_length_over_the_cap_is_refused_before_the_body_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Content-Length past the cap (and the form's envelope) is answered 413 at once: the app
    never asks for a byte of the body and never makes an upload folder (TD-033)."""
    from app.api import settings_data

    monkeypatch.setattr(settings_data, "MAX_IMPORT_BYTES", MIB)
    root = tmp_path / "ws"
    data = prepare(root, empty=True)
    app = make_app(root)

    async def small() -> Any:
        async def chunks() -> Any:
            yield b"{}"

        return chunks

    async def scenario() -> Any:
        async with app_lifespan(app):
            return await drive(
                app,
                IMPORT,
                multipart_stream(await small()),
                token=app.state.launch_token,
                headers={"content-length": str(50 * MIB)},
            ), folder_tree(data)

    answered, tree = asyncio.run(scenario())

    assert answered.status == 413, answered.body[:200]
    assert answered.delivered == 0, "no part of the body was read"
    message = error_tied_to(answered.body.decode(), "data-import-file", "data-import-error")
    assert "too large" in message and "200 MB" in message
    assert upload_folders(data) == [] and tree == folder_tree(data)


def test_import_upload_that_trickles_past_the_overall_deadline_is_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each wait is short enough for the read timeout, but the whole upload has a deadline too:
    a client that trickles bytes cannot hold the request (and the one import slot) for ever."""
    from app.api import settings_data

    monkeypatch.setattr(settings_data, "READ_TIMEOUT_S", 5.0)
    monkeypatch.setattr(settings_data, "UPLOAD_DEADLINE_S", 0.6)
    root = tmp_path / "ws"
    data = prepare(root, empty=True)
    app = make_app(root)
    valid = source_export(tmp_path / "source", rows=False)

    async def trickle() -> Any:
        async def chunks() -> Any:
            while True:
                yield b"0"
                await asyncio.sleep(0.1)  # a client that never goes silent for long

        return chunks

    async def pieces() -> Any:
        async def chunks() -> Any:
            for start in range(0, len(valid), 4096):
                yield valid[start : start + 4096]

        return chunks

    async def scenario() -> dict[str, Any]:
        async with app_lifespan(app):
            token = app.state.launch_token
            empty_tree = folder_tree(data)
            slow = await drive(
                app, IMPORT, multipart_stream(await trickle()), token=token, deadline=10.0
            )
            after = folder_tree(data) == empty_tree
            # The slot is free again: a normal upload goes through.
            again = await drive(app, IMPORT, multipart_stream(await pieces()), token=token)
            return {"slow": slow, "after": after, "again": again}

    found = asyncio.run(scenario())

    slow = found["slow"]
    assert slow.status == 422, slow.body[:200]
    assert error_tied_to(slow.body.decode(), "data-import-file", "data-import-error") == (
        UPLOAD_TOO_SLOW
    )
    assert found["after"] is True and upload_folders(data) == []
    assert found["again"].status == 200, found["again"].body[:200]


def test_import_allows_one_upload_at_a_time_and_releases_the_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second import while one is being received is answered 409 at once with a plain message
    (it reads nothing); the slot is released when the first one ends, here by stalling out, and
    the next import is taken."""
    from app.api import settings_data

    monkeypatch.setattr(settings_data, "READ_TIMEOUT_S", 2.0)
    root = tmp_path / "ws"
    data = prepare(root, empty=True)
    app = make_app(root)
    valid = source_export(tmp_path / "source", rows=False)

    async def scenario() -> dict[str, Any]:
        found: dict[str, Any] = {}
        started = asyncio.Event()

        async def first_chunks() -> Any:
            yield b'{"format": 1, "files": ['
            started.set()  # the app has the first bytes, so it holds the slot
            await asyncio.Event().wait()
            yield b""

        async def first_payload() -> Any:
            return first_chunks

        async def pieces() -> Any:
            async def chunks() -> Any:
                for start in range(0, len(valid), 4096):
                    yield valid[start : start + 4096]

            return chunks

        async with app_lifespan(app):
            token = app.state.launch_token
            first = asyncio.ensure_future(
                drive(
                    app,
                    IMPORT,
                    multipart_stream(await first_payload()),
                    token=token,
                    deadline=10.0,
                )
            )
            await asyncio.wait_for(started.wait(), 10.0)
            found["during"] = upload_folders(data)
            found["second"] = await drive(
                app, IMPORT, multipart_stream(await pieces()), token=token
            )
            found["second_delivered"] = found["second"].delivered
            found["during_after"] = upload_folders(data)
            found["first"] = await first  # stalls out and ends
            found["after_first"] = upload_folders(data)
            found["third"] = await drive(app, IMPORT, multipart_stream(await pieces()), token=token)
        return found

    found = asyncio.run(scenario())

    assert len(found["during"]) == 1, "the first upload is on its way"
    second = found["second"]
    assert second.status == 409, second.body[:200]
    assert second.delivered == 0, "the second upload was not read"
    assert error_tied_to(second.body.decode(), "data-import-file", "data-import-error") == (
        IMPORT_BUSY
    )
    assert found["during_after"] == found["during"], "the refused one made no folder of its own"
    assert found["first"].status == 422
    assert found["after_first"] == [] and upload_folders(data) == []
    assert found["third"].status == 200, found["third"].body[:300]


def test_import_disk_errors_are_answered_plainly_and_clean_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A full disk (or any OSError) while the upload is saved, or while the restore writes, is
    answered 422 with one fixed message. The log holds the exception's class only: never its
    message, a path or the file's content. The upload folder is removed and the slot is free."""
    from app.api import restore_upload
    from app.services import data_portability as portability_module

    caplog.set_level(logging.DEBUG)
    raw = source_export(tmp_path / "source", rows=False)
    cases: dict[str, Any] = {}

    def spool_fails(self: Any) -> None:
        raise OSError(28, f"No space left on device {DETAIL} {tmp_path}")

    def restore_fails(self: Any, path: Path) -> None:
        raise OSError(28, f"No space left on device {DETAIL} {path}")

    cases["spooling"] = (restore_upload._Sink, "write_pending", spool_fails)
    cases["restoring"] = (portability_module.DataPortabilityService, "restore", restore_fails)

    with running(tmp_path / "ws", empty=True) as run:
        data = tmp_path / "ws" / "data"
        before = state_of(run, data)
        for label, (owner, name, replacement) in cases.items():
            caplog.clear()
            with monkeypatch.context() as patched:
                patched.setattr(owner, name, replacement)
                answer = upload(run, raw, filename=PRIVATE_NAME)

            assert answer.status_code == 422, (label, answer.status_code, answer.text[:200])
            message = error_tied_to(answer.text, "data-import-file", "data-import-error")
            assert message == FILE_NOT_SAVED, label
            assert len(focus_targets(answer.text)) == 1
            ours = [r for r in caplog.records if r.name.startswith("app")]
            logged = " ".join(f"{r.getMessage()} {r.exc_text or ''}" for r in ours)
            assert "OSError" in logged, f"{label}: the class name is logged"
            shown = answer.text + " ".join(f"{k}: {v}" for k, v in answer.headers.items())
            for needle in (DETAIL, str(tmp_path), PRIVATE_NAME, "No space left"):
                assert needle not in logged and needle not in shown, (label, needle)
            assert all(record.exc_info is None for record in ours), (
                f"{label}: no traceback (its message and paths) is logged"
            )
            assert state_of(run, data) == before, f"{label}: nothing changed"
            assert upload_folders(data) == [], f"{label}: the upload folder was removed"

        # The slot was released each time: the same file now goes through.
        assert upload(run, raw).status_code == 200


def test_workspace_restored_attaches_the_runner_with_the_restored_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a restore the app hands the task runner the restored concurrency setting and
    computes provider health again (the other services read the database at call time)."""
    from app.domain.settings import TASK_RUNNER, TaskRunnerSettings
    from app.services.settings import SettingsService

    with running(tmp_path, empty=True) as run:
        state = run.app.state
        settings = SettingsService(state.workspace.db, FrozenClock(START))
        stored = settings.load(TASK_RUNNER)
        settings.save(
            TASK_RUNNER, TaskRunnerSettings(max_concurrent=3), expected_version=stored.version
        )
        attached: list[tuple[Any, int]] = []
        original = state.runner.attach

        def spy(service: Any, *, settings: TaskRunnerSettings) -> None:
            attached.append((service, settings.max_concurrent))
            original(service, settings=settings)

        refreshed: list[bool] = []
        monkeypatch.setattr(state.runner, "attach", spy)
        monkeypatch.setattr(state, "refresh_provider_health", lambda: refreshed.append(True))

        state.workspace_restored()

        assert attached == [(state.task_service, 3)], "re-attached with the restored setting"
        assert refreshed == [True], "provider health is computed again"


def test_import_route_calls_the_refresh_once_after_a_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = source_export(tmp_path / "source", rows=False)
    with running(tmp_path / "ws", empty=True) as run:
        calls: list[bool] = []
        monkeypatch.setattr(run.app.state, "workspace_restored", lambda: calls.append(True))

        answer = upload(run, raw)

        assert answer.status_code == 200 and calls == [True]
        assert one(answer.text, "data-import-done").text == (
            "Restored. Your data and files are back."
        )


def test_import_says_to_restart_when_the_refresh_after_a_restore_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The data is restored either way. If the app cannot take it in by itself, the answer says
    so and tells the person to restart; the log holds the exception's class only."""
    caplog.set_level(logging.DEBUG)
    raw = source_export(tmp_path / "source", rows=False)
    with running(tmp_path / "ws", empty=True) as run:
        data = tmp_path / "ws" / "data"

        def broken() -> None:
            raise RuntimeError(f"{DETAIL} {data}")

        monkeypatch.setattr(run.app.state, "workspace_restored", broken)

        answer = upload(run, raw)

        assert answer.status_code == 200, answer.text[:300]
        done = one(answer.text, "data-import-done").text
        assert done == (
            "Restored. Restart the app to finish: it couldn't refresh its setup state by itself."
        )
        assert announced(answer) == done
        assert len(focus_targets(answer.text)) == 1
        for relative, content in FILES.items():
            assert (data / relative).read_bytes() == content, "the data is restored"
        ours = [r for r in caplog.records if r.name.startswith("app")]
        logged = " ".join(f"{r.getMessage()} {r.exc_text or ''}" for r in ours)
        assert "RuntimeError" in logged
        assert all(r.exc_info is None for r in ours), "no traceback (paths) is logged"
        shown = answer.text + " ".join(f"{k}: {v}" for k, v in answer.headers.items())
        assert DETAIL not in logged and DETAIL not in shown and str(data) not in shown


def finish_setup(run: Any) -> None:
    """Mark every guide step done in the running app and refresh its setup decision."""
    registry = run.app.state.setup_registry
    state = SetupStateService(run.app.state.workspace.db, FrozenClock(START))
    progress = SetupProgress(registry, state)
    for step in registry.steps:
        progress.mark_done(step.id)
    run.app.state.refresh_provider_health()


def test_import_refreshes_the_setup_decision_from_the_restored_state(tmp_path: Path) -> None:
    """A restore replaces the setup state with the export's, and the app follows it at once
    (AC-M1.2): an export from a workspace whose guide was not finished sends the next request
    back to the guide; one from a finished workspace leaves the app open. No restart."""
    unfinished = source_export(tmp_path / "source", rows=False)
    with running(tmp_path / "a", empty=True) as run:
        assert run.client.get("/").status_code == 200, "control: the empty target is set up"
        assert upload(run, unfinished).status_code == 200
        assert run.app.state.onboarding.guide_step() is not None
        sent = run.client.get("/")
        assert sent.status_code == 303 and sent.headers["location"].startswith("/setup/")
        finish_setup(run)
        assert run.client.get("/").status_code == 200

    with running(tmp_path / "b", empty=False, files=True) as run_b:
        add_fact(run_b)
        name = generated_name(post(run_b, EXPORT, {}).text)
        finished = get_token(run_b, f"{PAGE}/exports/{name}").content
    with running(tmp_path / "c", empty=True) as run_c:
        assert upload(run_c, finished).status_code == 200
        assert run_c.app.state.onboarding.guide_step() is None
        assert run_c.client.get("/").status_code == 200


def test_data_service_calls_run_in_worker_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The services block (files, SQLite): the routes call them off the event loop (part B's
    accepted note; the upload read and the restore must not stall other requests)."""
    from app.services import data_portability as portability_module

    seen: dict[str, bool] = {}

    def off_loop(label: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen[label] = True
        else:
            seen[label] = False

    export = portability_module.DataPortabilityService.export
    restore = portability_module.DataPortabilityService.restore

    def watched_export(self: Any, *args: Any, **kwargs: Any) -> Any:
        off_loop("export")
        return export(self, *args, **kwargs)

    def watched_restore(self: Any, *args: Any, **kwargs: Any) -> Any:
        off_loop("restore")
        return restore(self, *args, **kwargs)

    monkeypatch.setattr(portability_module.DataPortabilityService, "export", watched_export)
    monkeypatch.setattr(portability_module.DataPortabilityService, "restore", watched_restore)
    raw = source_export(tmp_path / "source", rows=False)
    with running(tmp_path / "ws", empty=True) as run:
        erase = run.app.state.data_erasure.erase

        def watched_erase(confirmation: str) -> Any:
            off_loop("erase")
            return erase(confirmation)

        monkeypatch.setattr(run.app.state.data_erasure, "erase", watched_erase)
        assert upload(run, raw).status_code == 200
        # The restored setup state is the source's (no step done): every request would go to the
        # setup guide, which is right (AC-M1.2), so the guide is finished before the erase.
        finish_setup(run)
        assert post(run, EXPORT, {}).status_code == 200
        assert post(run, DELETE, {"confirm": "delete"}).status_code == 200
    assert seen == {"restore": True, "export": True, "erase": True}


# --- delete all data ---


def test_delete_route_needs_typed_confirmation_and_origin(tmp_path: Path) -> None:
    backend = MemoryKeyring()
    store = KeyringSecretStore(SERVICE_NAME, backend=backend)
    planted_keys(store)
    root = tmp_path / "ws"
    with running(root, empty=False, backend=backend, files=True) as run:
        data = root / "data"
        add_fact(run)
        before = state_of(run, data)
        keys = dict(backend.items)
        assert keys, "control: keys are planted"
        assert (root / "cfg" / "config.toml").is_file()

        # No token, no origin, a foreign origin: refused, nothing changed.
        for token, origin in ((False, True), (True, False), (True, "http://evil.example")):
            answer = post(run, DELETE, {"confirm": "delete"}, token=token, origin=origin)
            assert answer.status_code == 403, (token, origin)
        # Not a form this route knows.
        for bad in ({"confirm": ["delete", "delete"]}, {"confirm": "delete", "other": "1"}):
            answer = post(run, DELETE, bad)
            assert answer.status_code == 400 and answer.text == INVALID, bad
        # The word must be exactly "delete" (surrounding spaces aside).
        for word in ("", "del", "Delete", "DELETE", "deleted", "delete all data", "d e l e t e"):
            answer = post(run, DELETE, {"confirm": word})
            assert answer.status_code == 422, repr(word)
            assert "HX-Redirect" not in answer.headers
            assert one(answer.text, "data-delete-dialog").tag == "dialog"
            field = confirm_field(answer.text)
            assert field.attrs.get("aria-invalid") == "true"
            assert "data-delete-error" in (field.attrs.get("aria-describedby") or "").split()
            assert one(answer.text, "data-delete-error").attrs.get("role") == "alert"
        missing = post(run, DELETE, {})
        assert missing.status_code == 422 and "HX-Redirect" not in missing.headers
        assert state_of(run, data) == before, "a wrong word changes nothing"
        assert dict(backend.items) == keys and (root / "cfg" / "config.toml").is_file()
        assert run.app.state.workspace is not None
        assert (data / "backups").is_dir() and (data / "exports").is_dir()

        # The word, as typed (spaces around it are not part of it): everything goes.
        answer = post(run, DELETE, {"confirm": " delete "})
        assert answer.status_code == 200, answer.text[:300]
        assert answer.headers["HX-Redirect"] == "/"
        # The redirect drops any query string (ADR-050), so the browser is told by an event, and
        # app.js keeps the flag for the next page (sessionStorage).
        assert ERASED_EVENT in json.loads(answer.headers["HX-Trigger"])
        assert not (data / "app.db").exists()
        for name in ("backups", "documents", "imports", "assets", "prompts", "exports", "logs"):
            assert not (data / name).exists(), name
        assert not backend.items, "this workspace's keys are gone"
        for value in SECRET_VALUES.values():
            assert value not in backend.items.values()
        assert not (root / "cfg" / "config.toml").exists()
        assert run.app.state.workspace is None
        back = run.client.get("/")
        assert back.status_code == 303 and back.headers["location"].startswith("/setup/")
        # The setup page it lands on holds the confirmation, hidden until app.js shows it for
        # the one visit that follows a delete. Reading it needs no token and writes nothing.
        welcome = run.client.get(back.headers["location"])
        assert welcome.status_code == 200
        notice = one(welcome.text, "erased-notice")
        assert notice.text == ERASED_NOTICE and "hidden" in notice.attrs
        assert not (data / "app.db").exists(), "reading the page created nothing"


def test_delete_route_reports_incomplete_plainly(tmp_path: Path) -> None:
    from app.services.data_erasure import ErasureIncomplete, ErasureRefused, ErasureResult

    root = tmp_path / "ws"
    with running(root, empty=False) as run:
        data = root / "data"
        outcomes: list[Any] = [
            ErasureIncomplete(["app.db"], "exports", detail=f"{DETAIL} PermissionError {data}"),
            ErasureIncomplete(["app.db", "backups"], "key:claude_api_key", detail=DETAIL),
            ErasureIncomplete([], "config.toml", detail=f"{DETAIL} OSError {data}"),
            ErasureRefused('"backups" contains a link (symbolic or hard), nothing was deleted.'),
            ErasureResult((), (), (), (), False, ()),
        ]
        calls: list[str] = []

        class Stub:
            def erase(self, confirmation: str) -> Any:
                calls.append(confirmation)
                outcome = outcomes[len(calls) - 1]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        run.app.state.data_erasure = Stub()
        for index in range(4):
            answer = post(run, DELETE, {"confirm": "delete"})
            outcome = outcomes[index]
            assert 400 <= answer.status_code < 600, (index, answer.status_code)
            assert "HX-Redirect" not in answer.headers
            shown = page_text(answer.text)
            assert outcome.user_message in shown, (index, shown)
            assert one(answer.text, "data-delete-error").attrs.get("role") == "alert"
            assert one(answer.text, "data-delete-dialog").tag == "dialog", "retry from the dialog"
            assert str(data) not in answer.text and "PermissionError" not in answer.text
            assert "OSError" not in answer.text and DETAIL not in answer.text
            assert confirm_field(answer.text), "the form can be sent again"
        retry = post(run, DELETE, {"confirm": "delete"})  # the fifth call succeeds
        assert retry.status_code == 200 and retry.headers["HX-Redirect"] == "/"
        assert calls == ["delete"] * 5


# --- the page map ---


def test_data_pages_and_fragments_added_to_page_map_and_leak_scan(tmp_path: Path) -> None:
    """The page is registered as a page (so the dispatch test and the leak scan, which walk every
    page route without a parameter, reach it with no entry of their own); the fragments, the
    download and the POST routes are not pages; Settings links to it; and no page of the app shows
    the data folder's path."""
    root = tmp_path / "fictional-harbour-root-5521"
    with running(root, empty=False) as run:
        data = root / "data"
        routes = [r for r in run.app.routes if isinstance(r, APIRoute)]
        pages = [r for r in routes if PAGE_TAG in r.tags]
        mine = [r for r in pages if r.path == PAGE]
        assert len(mine) == 1 and {"GET", "HEAD"} <= mine[0].methods
        assert "{" not in mine[0].path, "no seeded id is needed in the dispatch test or leak map"
        assert not [r for r in pages if r.path.startswith(f"{PAGE}/")], "only the page is a page"
        others = {r.path for r in routes if r.path.startswith(f"{PAGE}/")}
        assert {LOCATION, DELETE, EXPORT, IMPORT} <= others
        assert [p for p in others if p.startswith(f"{PAGE}/exports/")], "the download route"
        assert not any(PAGE_TAG in r.tags for r in routes if r.path in others)

        settings = run.client.get("/settings").text
        links = [
            e
            for e in elements(settings)
            if e.tag == "a" and e.attrs.get("href") == PAGE and e.text == "Data and privacy"
        ]
        assert len(links) == 1, "Settings links to Data and privacy"

        needles = (str(data), str(data.resolve()), root.name)
        walked = 0
        for route in pages:
            if "{" in route.path or "GET" not in route.methods:
                continue
            for headers in ({}, run.htmx()):
                answer = run.client.get(route.path, headers=headers)
                walked += 1
                for needle in needles:
                    assert needle not in answer.text, f"{route.path} shows the folder path"
        assert walked >= 8, "the walk reached the app's pages"
