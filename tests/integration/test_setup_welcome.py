"""Step 1 of the setup guide: welcome and data folder (M1.16; AC-M1.1, AC-M1.6).

On a first run (source ``default``) nothing exists until step 1 is submitted: the POST creates
the chosen folder and its database, writes ``config.toml`` naming it, marks ``welcome`` done and
moves the guide to the AI provider step. A folder from ``--data-dir``, the environment or demo
mode is read-only, and a workspace that is already open is never opened again; then step 1 only
writes ``config.toml`` (never in demo mode, never over an existing one). A bootstrap naming a
folder that has gone offers "Locate the folder" or "Start fresh" (ADR-044, ADR-049, ADR-050,
ADR-052, ADR-053).

Submits are POST routes behind the launch token and origin checks. A valid submit answers 303 to
the next step, or for htmx 200 with the guide fragment, ``HX-Push-Url`` and an ``announce``
trigger. A refused folder answers 422 with the error tied to the field. Every folder lives under
``tmp_path`` and every value is fictional.
"""

import html
import json
import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx import Response

from app import __version__
from app.api.security import PAGE_TAG
from app.config import CliOptions, RuntimeConfig, load_bootstrap, resolve_config, write_bootstrap
from app.domain.errors import DataFolderUnavailable
from app.main import create_app
from app.modules.storage import Database, is_workspace_database
from app.onboarding.detection import StartupDecision
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import m1_registry
from app.services.setup_state import SetupStateService
from tests.fakes.clock import FrozenClock
from tests.integration.test_setup_redirect import (
    BASE_URL,
    ORIGIN,
    PORT,
    START,
    Element,
    Running,
    assert_see_other,
    outline,
)
from tests.setup_harness import seed_workspace

pytestmark = pytest.mark.integration

WELCOME = "/setup/welcome"
PROVIDER = "/setup/provider"
NEXT_ANNOUNCEMENT = "Step 2 of 4: AI provider"
DEMO_COMMAND = "uv run jobpipeline --demo"
STEP_TITLES = [step.title for step in m1_registry().steps]


# --- helpers -------------------------------------------------------------------------------


def make_config(tmp_path: Path, source: str, *, data_dir: Path | None = None) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=data_dir if data_dir is not None else tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=PORT,
        log_level="INFO",
        demo=source == "demo",
        setup=False,
        no_browser=False,
        data_dir_source=source,  # type: ignore[arg-type]
    )


@contextmanager
def started(config: RuntimeConfig) -> Iterator[Running]:
    app = create_app(config, clock=FrozenClock(START))
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        yield Running(app=app, client=client, hits=Counter())


@contextmanager
def running(
    tmp_path: Path, source: str = "default", *, data_dir: Path | None = None
) -> Iterator[Running]:
    with started(make_config(tmp_path, source, data_dir=data_dir)) as run:
        yield run


def submit(
    run: Running, folder: str | None = None, *, action: str | None = None, htmx: bool = False
) -> Response:
    form: dict[str, str] = {}
    if folder is not None:
        form["folder"] = folder
    if action is not None:
        form["action"] = action
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(WELCOME, data=form, headers=headers)


def bootstrap_file(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / "config.toml"


def bootstrap_folder(tmp_path: Path) -> Path | None:
    loaded = load_bootstrap(tmp_path / "cfg")
    return loaded.data_dir if loaded is not None else None


def stored_statuses(data_dir: Path) -> dict[str, str]:
    """The setup_state rows of a closed workspace (only call when ``app.db`` exists)."""
    assert (data_dir / "app.db").is_file(), data_dir
    with Database.open(data_dir / "app.db") as db:
        progress = SetupProgress(m1_registry(), SetupStateService(db, FrozenClock(START)))
        return dict(progress.statuses())


def live_statuses(run: Running) -> dict[str, str]:
    db: Database = run.app.state.workspace.db
    progress = SetupProgress(
        run.app.state.setup_registry, SetupStateService(db, FrozenClock(START))
    )
    return dict(progress.statuses())


def snapshot(root: Path) -> list[Path]:
    return sorted(path.relative_to(root) for path in root.rglob("*"))


def only(elements: list[Element], description: str) -> Element:
    assert len(elements) == 1, f"expected one {description}, found {len(elements)}"
    return elements[0]


def folder_input(elements: list[Element]) -> Element:
    return only(
        [e for e in elements if e.tag == "input" and e.attrs.get("id") == "folder"],
        'input id="folder"',
    )


def by_id(elements: list[Element], element_id: str) -> Element | None:
    return next((e for e in elements if e.attrs.get("id") == element_id), None)


def described_by(elements: list[Element], field: Element) -> list[Element]:
    ids = (field.attrs.get("aria-describedby") or "").split()
    return [e for e in (by_id(elements, i) for i in ids) if e is not None]


def submit_buttons(elements: list[Element]) -> dict[str, Element]:
    """Submit buttons named ``action``, by value."""
    return {
        str(e.attrs.get("value")): e
        for e in elements
        if e.tag == "button"
        and e.attrs.get("name") == "action"
        and (e.attrs.get("type") or "submit") == "submit"
    }


def assert_field_error(response: Response, submitted: str | None) -> None:
    """422, the folder field marked invalid and described by a visible message."""
    assert response.status_code == 422, (response.status_code, response.text[:300])
    elements = outline(response.text)
    field = folder_input(elements)
    assert field.attrs.get("aria-invalid") == "true"
    messages = [e for e in described_by(elements, field) if e.text]
    assert messages, "the error is tied to the field with aria-describedby"
    if submitted is not None:
        assert field.attrs.get("value") == submitted, "the submitted text is shown back"


def assert_step_done(response: Response) -> None:
    assert_see_other(response, PROVIDER)


def is_readonly(field: Element) -> bool:
    return "readonly" in field.attrs


# --- AC-M1.1 case 1: first run -----------------------------------------------------------------


def test_ac_m1_1_first_run_root_redirects_to_welcome(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        assert run.decision == StartupDecision("guide", "welcome")
        assert_see_other(run.client.get("/"), WELCOME)
        page = run.client.get(WELCOME)
        assert page.status_code == 200, page.text[:300]
        elements = outline(page.text)
        folder_input(elements)
        assert run.app.state.workspace is None

    assert not (tmp_path / "data").exists(), "a first run creates nothing before step 1"
    assert not (tmp_path / "cfg").exists()


def test_welcome_suggests_default_folder(tmp_path: Path) -> None:
    suggested = tmp_path / "data"
    with running(tmp_path, "default", data_dir=suggested) as run:
        page = run.client.get(WELCOME)

    assert page.status_code == 200
    elements = outline(page.text)
    heading = only([e for e in elements if e.attrs.get("id") == "step-heading"], "#step-heading")
    assert heading.tag == "h2"
    assert heading.text == "Welcome and data folder"

    field = folder_input(elements)
    assert field.attrs.get("name") == "folder"
    assert field.attrs.get("value") == str(suggested)
    assert not is_readonly(field), "a suggested default can be changed"
    labels = [e for e in elements if e.tag == "label" and e.attrs.get("for") == "folder"]
    assert only(labels, 'label for="folder"').text, "the field has a visible label"

    buttons = submit_buttons(elements)
    assert set(buttons) == {"open"}, sorted(buttons)

    codes = [e for e in elements if e.tag == "code" and e.text == DEMO_COMMAND]
    assert codes, "Try the demo shows the command"

    step = by_id(elements, "setup-step")
    assert step is not None
    assert "this computer" in step.text.lower(), "the step explains what stays local"
    assert "provider" in step.text.lower(), "and what is sent to the AI provider"
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("source", ["cli", "environment"])
def test_welcome_explicit_data_dir_is_read_only(tmp_path: Path, source: str) -> None:
    explicit = tmp_path / "explicit"
    elsewhere = tmp_path / "elsewhere"
    with running(tmp_path, source, data_dir=explicit) as run:
        workspace = run.app.state.workspace
        elements = outline(run.client.get(WELCOME).text)
        field = folder_input(elements)
        assert field.attrs.get("value") == str(explicit)
        assert is_readonly(field)
        hints = [e for e in described_by(elements, field) if e.text]
        assert hints, "a read-only folder explains why (aria-describedby)"

        assert_step_done(submit(run, str(elsewhere), action="open"))

        assert run.app.state.workspace is workspace, "the open workspace is never reopened"
        assert live_statuses(run).get("welcome") == "done"

    assert not elsewhere.exists(), "a folder sent with a read-only field is ignored"
    assert bootstrap_folder(tmp_path) == explicit
    assert stored_statuses(explicit)["welcome"] == "done"


# --- AC-M1.6: submit saves and a restart resumes -----------------------------------------------


def test_ac_m1_6_welcome_submit_creates_db_writes_bootstrap_marks_done(tmp_path: Path) -> None:
    chosen = tmp_path / "my-job-data"
    with running(tmp_path, "default") as run:
        assert run.app.state.workspace is None
        assert_step_done(submit(run, str(chosen), action="open"))

        assert chosen.is_dir()
        assert (chosen / "app.db").is_file()
        assert is_workspace_database(chosen / "app.db")
        assert run.app.state.workspace.data_dir == chosen
        assert live_statuses(run) == {"welcome": "done"}
        assert run.decision == StartupDecision("guide", "provider")
        assert_see_other(run.client.get("/"), PROVIDER)

    assert bootstrap_folder(tmp_path) == chosen
    assert not (tmp_path / "data").exists(), "only the chosen folder is created"

    # A restart reads config.toml like the CLI does and resumes at the next unfinished step.
    restarted = resolve_config(
        CliOptions(port=PORT), {"JOBPIPELINE_CONFIG_DIR": str(tmp_path / "cfg")}
    )
    assert restarted.data_dir == chosen
    assert restarted.data_dir_source == "bootstrap"
    with started(restarted) as run:
        assert run.decision == StartupDecision("guide", "provider")
        assert_see_other(run.client.get("/"), PROVIDER)
        assert_see_other(run.client.get("/setup"), PROVIDER)


def test_welcome_submit_without_action_opens_the_folder(tmp_path: Path) -> None:
    """``action`` defaults to ``open``."""
    chosen = tmp_path / "chosen"
    with running(tmp_path, "default") as run:
        assert_step_done(submit(run, str(chosen)))
    assert bootstrap_folder(tmp_path) == chosen
    assert stored_statuses(chosen) == {"welcome": "done"}


def test_welcome_accepts_empty_folder_and_existing_workspace(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with running(tmp_path / "first", "default") as run:
        assert_step_done(submit(run, str(empty), action="open"))
    assert (empty / "app.db").is_file()

    existing = tmp_path / "existing"
    seed_workspace(existing, clock=FrozenClock(START))
    with running(tmp_path / "second", "default") as run:
        assert_step_done(submit(run, str(existing), action="open"))
    assert bootstrap_folder(tmp_path / "second") == existing
    assert stored_statuses(existing)["welcome"] == "done"


# --- refused folders ---------------------------------------------------------------------------


def _foreign_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "holiday-photos"
    folder.mkdir()
    (folder / "notes.txt").write_text("fictional notes\n", encoding="utf-8")
    return folder


def _a_file(tmp_path: Path) -> Path:
    path = tmp_path / "a-file.txt"
    path.write_text("fictional\n", encoding="utf-8")
    return path


def _foreign_database_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "other-app"
    folder.mkdir()
    connection = sqlite3.connect(folder / "app.db")
    try:
        connection.execute("CREATE TABLE fictional_notes (id INTEGER PRIMARY KEY, body TEXT)")
        connection.commit()
    finally:
        connection.close()
    return folder


REFUSED = {
    "relative": lambda tmp: "relative/job-data",
    "dot_relative": lambda tmp: "./job-data",
    "file": lambda tmp: str(_a_file(tmp)),
    "empty": lambda tmp: "",
    "blank": lambda tmp: "   ",
    "not_empty": lambda tmp: str(_foreign_folder(tmp)),
    "foreign_sqlite": lambda tmp: str(_foreign_database_folder(tmp)),
    "config_folder": lambda tmp: str(tmp / "cfg"),
    "nul": lambda tmp: str(tmp / "job\x00data"),
}
# Values echoed back exactly (blank may be stripped; NUL can't be shown in HTML).
ECHOED = {"relative", "dot_relative", "file", "not_empty", "foreign_sqlite", "config_folder"}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_welcome_rejects_relative_path_or_file_with_field_error(tmp_path: Path, case: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    folder = REFUSED[case](root)
    with running(root, "default") as run:
        before = snapshot(root)
        response = submit(run, folder, action="open")

        assert_field_error(response, folder if case in ECHOED else None)
        assert snapshot(root) == before, "a refused folder creates nothing"
        assert run.app.state.workspace is None
        assert run.decision == StartupDecision("guide", "welcome")
        assert_see_other(run.client.get("/"), WELCOME)

    assert not bootstrap_file(root).exists()
    if case in ("not_empty", "foreign_sqlite"):
        assert not (Path(folder) / "logs").exists()
        assert not (Path(folder) / "backups").exists()


def test_welcome_missing_folder_field_is_an_error(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        response = submit(run, None, action="open")
        assert_field_error(response, None)
    assert snapshot(tmp_path) == []


def test_welcome_error_htmx_fragment_and_plain_page(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        plain = submit(run, "relative/job-data")
        assert_field_error(plain, "relative/job-data")
        assert "<html" in plain.text, "a plain request gets the full page"

        fragment = submit(run, "relative/job-data", htmx=True)
        assert_field_error(fragment, "relative/job-data")
        for marker in ("<html", "<body", 'id="announcer"'):
            assert marker not in fragment.text, marker
        assert "hx-push-url" not in fragment.headers
        elements = outline(fragment.text)
        current = only([e for e in elements if e.attrs.get("aria-current") == "step"], "current")
        assert current.text == "Welcome and data folder"


def test_welcome_error_echo_is_escaped(tmp_path: Path) -> None:
    hostile = '"><script>alert(1)</script>'
    with running(tmp_path, "default") as run:
        response = submit(run, hostile)
    assert_field_error(response, hostile)
    assert "<script>alert(1)" not in response.text


def test_welcome_unknown_action_is_a_form_error(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        response = submit(run, str(tmp_path / "chosen"), action="delete-everything")
        assert response.status_code == 422, response.text[:300]
        field = folder_input(outline(response.text))
        assert field.attrs.get("aria-invalid") != "true", "not a folder problem"
        assert run.app.state.workspace is None
    assert snapshot(tmp_path) == []


def test_welcome_open_failure_shows_plain_message_and_allows_retry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = _a_file(tmp_path)
    unusable = blocker / "inside"  # can't be created: its parent is a file
    good = tmp_path / "good"
    with caplog.at_level(logging.DEBUG), running(tmp_path, "default") as run:
        response = submit(run, str(unusable))
        assert_field_error(response, str(unusable))
        assert DataFolderUnavailable().user_message in html.unescape(response.text)
        assert run.app.state.workspace is None
        assert not bootstrap_file(tmp_path).exists()

        assert_step_done(submit(run, str(good)))
        assert run.app.state.workspace.data_dir == good

    assert bootstrap_folder(tmp_path) == good
    for record in caplog.records:
        assert str(unusable) not in record.getMessage(), "the folder path is never logged"


# --- AC-M1.1 case 2: the folder named in config.toml has gone ----------------------------------


def _moved_bootstrap(tmp_path: Path) -> Path:
    moved = tmp_path / "moved-away"
    write_bootstrap(tmp_path / "cfg", data_dir=moved, version=__version__)
    return moved


def test_ac_m1_1_moved_folder_offers_locate_or_start_fresh(tmp_path: Path) -> None:
    moved = _moved_bootstrap(tmp_path)
    with running(tmp_path, "bootstrap", data_dir=moved) as run:
        assert run.decision.kind == "locate_or_fresh"
        page = run.client.get(WELCOME)
        assert page.status_code == 200
        fragment = run.client.get(WELCOME, headers=run.htmx())
        assert fragment.status_code == 200

    for response in (page, fragment):
        elements = outline(response.text)
        buttons = submit_buttons(elements)
        assert set(buttons) == {"locate", "fresh"}, sorted(buttons)
        assert buttons["locate"].text == "Locate the folder"
        assert buttons["fresh"].text == "Start fresh"
        field = folder_input(elements)
        assert field.attrs.get("value") == str(moved)
        assert not is_readonly(field)
    assert not moved.exists()


def test_locate_existing_folder_updates_bootstrap(tmp_path: Path) -> None:
    moved = _moved_bootstrap(tmp_path)
    found = tmp_path / "found-again"
    seed_workspace(found, clock=FrozenClock(START))
    empty = tmp_path / "empty"
    empty.mkdir()
    original = bootstrap_file(tmp_path).read_bytes()

    with running(tmp_path, "bootstrap", data_dir=moved) as run:
        for refused in (empty, moved, _foreign_folder(tmp_path)):
            response = submit(run, str(refused), action="locate")
            assert_field_error(response, str(refused))
            assert bootstrap_file(tmp_path).read_bytes() == original
            assert run.app.state.workspace is None
        assert not moved.exists()
        assert list(empty.iterdir()) == [], "locating an empty folder creates nothing"

        assert_step_done(submit(run, str(found), action="locate"))
        assert run.app.state.workspace.data_dir == found
        assert run.decision == StartupDecision("guide", "provider")

    assert bootstrap_folder(tmp_path) == found, "config.toml now names the located folder"
    assert stored_statuses(found)["welcome"] == "done"
    assert not moved.exists()


def test_start_fresh_creates_empty_database(tmp_path: Path) -> None:
    moved = _moved_bootstrap(tmp_path)
    workspace = tmp_path / "has-data"
    seed_workspace(workspace, done=("welcome",), clock=FrozenClock(START))

    with running(tmp_path, "bootstrap", data_dir=moved) as run:
        # Start fresh never adopts a folder that holds data.
        for refused in (workspace, _foreign_folder(tmp_path)):
            assert_field_error(submit(run, str(refused), action="fresh"), str(refused))
        assert run.app.state.workspace is None

        assert_step_done(submit(run, str(moved), action="fresh"))
        assert run.app.state.workspace.data_dir == moved

    assert is_workspace_database(moved / "app.db")
    assert stored_statuses(moved) == {"welcome": "done"}
    assert bootstrap_folder(tmp_path) == moved


# --- a workspace already open: write config.toml only ------------------------------------------


def test_demo_mode_never_writes_bootstrap(tmp_path: Path) -> None:
    demo_dir = tmp_path / "demo-data"
    with running(tmp_path, "demo", data_dir=demo_dir) as run:
        elements = outline(run.client.get(WELCOME).text)
        assert is_readonly(folder_input(elements))
        assert DEMO_COMMAND not in run.client.get(WELCOME).text, "no Try the demo inside demo"

        assert_step_done(submit(run, str(tmp_path / "elsewhere")))
        assert live_statuses(run).get("welcome") == "done"
        assert run.decision == StartupDecision("guide", "provider")

    assert not bootstrap_file(tmp_path).exists()
    assert not (tmp_path / "elsewhere").exists()
    assert stored_statuses(demo_dir)["welcome"] == "done"


def test_existing_database_without_bootstrap_only_writes_bootstrap(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    seed_workspace(data_dir, clock=FrozenClock(START))
    other = tmp_path / "other"

    with running(tmp_path, "default", data_dir=data_dir) as run:
        workspace = run.app.state.workspace
        assert workspace is not None, "an existing database is opened at start-up"
        field = folder_input(outline(run.client.get(WELCOME).text))
        assert is_readonly(field)
        assert field.attrs.get("value") == str(data_dir)

        assert_step_done(submit(run, str(other), action="open"))
        assert run.app.state.workspace is workspace, "never opened a second time"

    assert bootstrap_folder(tmp_path) == data_dir
    assert not other.exists()
    assert stored_statuses(data_dir)["welcome"] == "done"


@pytest.mark.parametrize("source", ["cli", "environment"])
def test_explicit_folder_never_overwrites_existing_bootstrap(tmp_path: Path, source: str) -> None:
    original_folder = tmp_path / "original"
    write_bootstrap(tmp_path / "cfg", data_dir=original_folder, version=__version__)
    original = bootstrap_file(tmp_path).read_bytes()
    explicit = tmp_path / "explicit"

    with running(tmp_path, source, data_dir=explicit) as run:
        assert_step_done(submit(run))

    assert bootstrap_file(tmp_path).read_bytes() == original
    assert stored_statuses(explicit)["welcome"] == "done"
    assert not original_folder.exists()


def test_resubmit_after_open_never_reopens(tmp_path: Path) -> None:
    chosen = tmp_path / "chosen"
    other = tmp_path / "other"
    with running(tmp_path, "default") as run:
        assert_step_done(submit(run, str(chosen)))
        workspace = run.app.state.workspace
        written = bootstrap_file(tmp_path).read_bytes()

        field = folder_input(outline(run.client.get(WELCOME).text))
        assert is_readonly(field), "once a workspace is open the folder is fixed"
        assert field.attrs.get("value") == str(chosen)

        assert_step_done(submit(run, str(other), action="open"))
        assert run.app.state.workspace is workspace
        assert bootstrap_file(tmp_path).read_bytes() == written

    assert not other.exists()


# --- route requirements --------------------------------------------------------------------------


def test_welcome_post_needs_token_and_origin(tmp_path: Path) -> None:
    chosen = str(tmp_path / "chosen")
    with running(tmp_path, "default") as run:
        no_token = run.client.post(WELCOME, data={"folder": chosen}, headers={"origin": ORIGIN})
        assert no_token.status_code == 403
        no_token_htmx = run.client.post(
            WELCOME, data={"folder": chosen}, headers={"origin": ORIGIN, "HX-Request": "true"}
        )
        assert no_token_htmx.status_code == 403
        bad_origin = run.client.post(
            WELCOME, data={"folder": chosen}, headers=run.action(origin="http://evil.example")
        )
        assert bad_origin.status_code == 403
        no_origin = run.client.post(WELCOME, data={"folder": chosen}, headers=run.with_token())
        assert no_origin.status_code == 403
        assert run.app.state.workspace is None

    assert snapshot(tmp_path) == []


def test_welcome_get_never_submits(tmp_path: Path) -> None:
    chosen = tmp_path / "chosen"
    with running(tmp_path, "default") as run:
        response = run.client.get(WELCOME, params={"folder": str(chosen), "action": "open"})
        assert response.status_code == 200
        assert run.app.state.workspace is None
    assert snapshot(tmp_path) == []


def test_unknown_step_post_is_404(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        response = run.client.post(
            "/setup/fictional-step", data={"folder": "x"}, headers=run.action()
        )
        assert response.status_code == 404
    assert snapshot(tmp_path) == []


def test_welcome_submit_route_is_not_page_tagged(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        posts = [
            route
            for route in run.app.router.routes
            if isinstance(route, APIRoute)
            and route.path == "/setup/{step_id}"
            and "POST" in route.methods
        ]
        assert posts, "a POST route for /setup/{step_id}"
        for route in posts:
            assert PAGE_TAG not in route.tags


def test_htmx_config_swaps_422_and_keeps_other_errors(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        page = run.client.get(WELCOME)
    meta = only(
        [e for e in outline(page.text) if e.tag == "meta" and e.attrs.get("name") == "htmx-config"],
        "htmx-config meta",
    )
    config = json.loads(str(meta.attrs.get("content")))
    assert config["selfRequestsOnly"] is True, "the hardened settings stay (ADR-041)"
    handling = config["responseHandling"]

    def first_match(status: str) -> dict[str, object]:
        # htmx tests each entry's code as a regular expression, first match wins.
        return next(entry for entry in handling if re.search(str(entry["code"]), status))

    assert first_match("422").get("swap") is True
    assert first_match("422").get("error") is not True
    for status in ("400", "403", "404", "500"):
        assert first_match(status).get("error") is True, status
    assert first_match("200").get("swap") is True


# --- htmx: guide fragment, push URL, title and announcement ---------------------------------------


def test_welcome_fragment_for_htmx_request(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        full = run.client.get(WELCOME)
        fragment = run.client.get(WELCOME, headers=run.htmx())
        posted = submit(run, str(tmp_path / "data"), htmx=True)

    # The full page holds the guide partial inside #setup-guide.
    full_elements = outline(full.text)
    guide = by_id(full_elements, "setup-guide")
    assert guide is not None and guide.tag == "div"
    step_lists = [e for e in full_elements if e.tag == "ol" and e.within(guide)]
    assert len(step_lists) == 1
    step_section = by_id(full_elements, "setup-step")
    assert step_section is not None and step_section.within(guide)

    # GET with HX-Request: the guide fragment (step list and form), not a page.
    assert fragment.status_code == 200, fragment.text[:300]
    for marker in ("<html", "<body", 'id="announcer"'):
        assert marker not in fragment.text, marker
    elements = outline(fragment.text)
    lists = [e for e in elements if e.tag == "ol" and all(t in e.text for t in STEP_TITLES)]
    assert len(lists) == 1, "the fragment re-renders the step list"
    current = only([e for e in elements if e.attrs.get("aria-current") == "step"], "current")
    assert current.text == "Welcome and data folder"
    assert current.within(lists[0])
    assert by_id(elements, "setup-step") is not None
    folder_input(elements)

    # POST via htmx: the next step's guide fragment with push URL, title and announcement.
    assert posted.status_code == 200, posted.text[:300]
    assert "location" not in posted.headers
    assert posted.headers.get("hx-push-url") == PROVIDER
    assert json.loads(posted.headers["hx-trigger"]) == {"announce": NEXT_ANNOUNCEMENT}
    for marker in ("<html", "<body", 'id="announcer"'):
        assert marker not in posted.text, marker
    posted_elements = outline(posted.text)
    title = only([e for e in posted_elements if e.tag == "title"], "<title>")
    assert title.text.startswith("AI provider"), title.text
    current = only(
        [e for e in posted_elements if e.attrs.get("aria-current") == "step"], "current step"
    )
    assert current.text == "AI provider"
    assert by_id(posted_elements, "step-heading") is not None
    assert bootstrap_folder(tmp_path) == tmp_path / "data"


# --- review follow-ups (ADR-060) ----------------------------------------------------------------


def test_default_folder_works_when_config_folder_is_the_data_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # macOS and Windows: platformdirs gives one folder for config and data, so config.toml
    # sits beside app.db. The default folder must still be accepted, also for "Start fresh".
    monkeypatch.setattr("app.config.user_data_dir", lambda name: str(tmp_path / "platform" / name))
    shared = tmp_path / "platform" / "jobpipeline"
    config = RuntimeConfig(
        data_dir=shared,
        config_dir=shared,
        port=PORT,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
        data_dir_source="default",
    )
    with started(config) as run:
        assert_step_done(submit(run, str(shared)))
    loaded = load_bootstrap(shared)
    assert loaded is not None and loaded.data_dir == shared
    assert stored_statuses(shared)["welcome"] == "done"

    # The database goes; config.toml stays beside it: "Start fresh" there is accepted.
    for path in shared.iterdir():
        if path.name != "config.toml":
            if path.is_dir():
                for inner in sorted(path.rglob("*"), reverse=True):
                    inner.rmdir() if inner.is_dir() else inner.unlink()
                path.rmdir()
            else:
                path.unlink()
    restarted = resolve_config(CliOptions(), {"JOBPIPELINE_CONFIG_DIR": str(shared)})
    with started(restarted) as run:
        assert run.decision.kind == "locate_or_fresh"
        assert_step_done(submit(run, str(shared), action="fresh"))
    assert is_workspace_database(shared / "app.db")


def test_done_step_shows_done_in_text(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        posted = submit(run, str(tmp_path / "data"), htmx=True)
    welcome_item = only(
        [
            e
            for e in outline(posted.text)
            if e.tag == "li" and e.text.startswith("Welcome and data folder")
        ],
        "welcome item",
    )
    assert "Done" in welcome_item.text


def test_refused_submit_title_says_error(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        fragment = submit(run, "relative/job-data", htmx=True)
        page = submit(run, "relative/job-data")
    for response in (fragment, page):
        title = only([e for e in outline(response.text) if e.tag == "title"], "<title>")
        assert title.text.startswith("Error: Welcome and data folder"), title.text


def test_refused_submit_echoes_only_the_folder(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:
        response = run.client.post(
            WELCOME,
            data={
                "folder": "relative/job-data",
                "mode": "locate_or_fresh",
                "demo_command": "fictional-injected-command",
            },
            headers=run.action(),
        )
    assert_field_error(response, "relative/job-data")
    assert "fictional-injected-command" not in response.text
    assert set(submit_buttons(outline(response.text))) == {"open"}


def test_request_validation_error_is_plain_400(tmp_path: Path) -> None:
    with running(tmp_path, "default") as run:

        def typed(count: int) -> dict[str, int]:
            return {"count": count}

        run.app.add_api_route("/setup/fictional/typed", typed, methods=["GET"])
        response = run.client.get(
            "/setup/fictional/typed?count=<img src=x onerror=alert(1)>", headers=run.with_token()
        )
    assert response.status_code == 400
    assert "onerror" not in response.text
