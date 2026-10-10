"""Task status routes and fragments (M2.19; AC-M2.2, AC-M2.4; spec 2.9 rule 12, 2.11; ADR-076
item 11, ADR-077).

Assumed interface:

- ``GET /tasks/panel`` (token; fragment only): root ``#tasks-panel`` with
  ``hx-get="/tasks/panel"``, ``hx-trigger="tasks:resync from:body, tasks:updated from:body"``
  and ``hx-swap="outerHTML"``. It holds one ``#task-<id>`` per recent task, most recently
  changed first, or ``#tasks-empty`` when there are none.
- ``GET /tasks/{id}`` (token; fragment only): root ``#task-<id>`` with ``hx-get`` on its own
  URL and the same trigger and swap. Inside: ``#task-<id>-label`` (the registry's label, or
  "Task" for a type no longer registered), ``#task-<id>-status`` (Queued, Running,
  Cancelling…, Done, Failed, Cancelled, Interrupted), for queued and running tasks a
  ``<progress id="task-<id>-progress" max="100">`` with an accessible name,
  ``#task-<id>-reason`` (failed and interrupted only: the spec 2.11 message for the stored
  kind, never the stored text), ``#task-<id>-result`` when the task has a result reference,
  a Cancel button (queued, or running without a cancel request) and a Retry button (failed,
  cancelled, interrupted). Buttons post with ``hx-post``, ``hx-target="#task-<id>"`` and
  ``hx-swap="outerHTML"``.
- ``POST /tasks/{id}/cancel`` and ``POST /tasks/{id}/retry`` (token and origin) answer the
  task fragment: 200, or 409 with ``TaskNotRetryable``'s message in ``#task-<id>-reason``
  when the retry is refused. A retry that finds an active duplicate answers that task.
- An unknown id answers 404 with a short fixed body that never repeats the id.
- ``app.domain.errors.message_for_kind(kind)`` maps a stored kind to its message.
- Templates live under ``app/templates/fragments/tasks/``.

Rows are seeded straight through the tasks repository on the app's own database, and the
runner is kept asleep, so every row stays in the state the test gave it. Real SQLite under
``tmp_path``; ``FrozenClock``; sequential ids; every value is fictional. New names are
imported inside the tests.
"""

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import Response

from app.api.security import TOKEN_HEADER
from app.domain.errors import (
    AppError,
    InvalidModelOutput,
    NetworkUnavailable,
    TaskNotRetryable,
    UsageLimitReached,
)
from app.domain.tasks import TaskStatus, TaskTypeSpec, dedupe_key
from app.modules.storage import tasks_repo
from app.tasks import HandlerRegistry
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_practice_task import Practice, by_id, practising
from tests.integration.test_setup_redirect import ORIGIN, Element, outline

pytestmark = pytest.mark.integration

PANEL = "/tasks/panel"
PANEL_ID = "tasks-panel"
EMPTY_ID = "tasks-empty"
TRIGGER = "tasks:resync from:body, tasks:updated from:body"
ECHO, ECHO_LABEL = "fictional_echo", "Echo check"
DRAFT, DRAFT_LABEL = "fictional_draft", "Draft letter"
GONE = "fictional_gone"  # stored by an "older version": no handler registered
NEUTRAL_LABEL = "Task"
WORDS = {
    "queued": "Queued",
    "running": "Running",
    "succeeded": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "interrupted": "Interrupted",
}
CANCELLING = "Cancelling…"
MSG_EMPTY = "No tasks yet. AI work you start shows up here."
MSG_INTERRUPTED = (
    "This task stopped before it finished, for example because the app closed. "
    "Retry to run it again."
)
MSG_BUDGET = (
    "A monthly usage limit was reached. Open Settings > Usage budgets to see when it resets."
)
GENERIC = AppError.message
HOSTILE = '<script>alert("fictional-leak")</script> /Users/fictional/notes sk-fictional-0000'
HOSTILE_PARTS = ("<script", "alert(", "fictional-leak", "/Users/fictional", "sk-fictional")
ITEM_ID = re.compile(r"task-(t-\d{4})")
MISSING_IDS = ("t-9999", "fictional-missing-7f3a9", "%3Cb%3Efictional-markup%3C%2Fb%3E")


# --- helpers -----------------------------------------------------------------------------------


async def _never_runs(ctx: Any) -> str | None:  # pragma: no cover - the runner stays asleep
    raise AssertionError("no seeded task may run")


def _registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    for task_type, label in ((ECHO, ECHO_LABEL), (DRAFT, DRAFT_LABEL)):
        registry.register(
            task_type, TaskTypeSpec(timeout=timedelta(minutes=30), label=label), _never_runs
        )
    return registry


class Tasks:
    """The running app plus a seeder writing task rows through the repository."""

    def __init__(self, practice: Practice) -> None:
        self.practice = practice
        self.run = practice.run
        self.db = practice.app.state.workspace.db
        self.ids = SequentialIdFactory("t")

    def _now(self) -> str:
        self.practice.clock.advance(timedelta(seconds=1))
        return str(self.practice.clock.now().isoformat())

    def add(
        self,
        status: str = "queued",
        *,
        task_type: str = ECHO,
        target: str | None = None,
        error: str | None = None,
        result_ref: str | None = None,
        progress: int | None = None,
        cancel_requested: bool = False,
    ) -> str:
        """One row in ``status``; each write is one second later than the last, so the order
        of changes is the order of the calls."""
        task_id = self.ids.new()
        now = self._now()
        row = tasks_repo.TaskRow(
            id=task_id,
            type=task_type,
            status=TaskStatus.QUEUED,
            dedupe_key=dedupe_key(task_type, target or task_id),
            attempt=1,
            progress=0,
            heartbeat_at=None,
            cancel_requested=False,
            input_ref=None,
            result_ref=None,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=None,
            version=1,
        )
        wanted = TaskStatus(status)
        with self.db.transaction() as conn:
            _row, created = tasks_repo.insert_or_get_active(conn, row)
            assert created, "seeded with a free dedupe key"
            if wanted == TaskStatus.QUEUED:
                return task_id
            if wanted == TaskStatus.CANCELLED:
                assert tasks_repo.transition(conn, task_id, [TaskStatus.QUEUED], wanted, now=now)
                return task_id
            assert tasks_repo.transition(
                conn, task_id, [TaskStatus.QUEUED], TaskStatus.RUNNING, now=now
            )
            if progress is not None:
                assert tasks_repo.heartbeat(conn, task_id, now, progress=progress)
            if cancel_requested:
                assert tasks_repo.request_cancel(conn, task_id, now)
            if wanted != TaskStatus.RUNNING:
                assert tasks_repo.transition(
                    conn,
                    task_id,
                    [TaskStatus.RUNNING],
                    wanted,
                    now=now,
                    error=error,
                    result_ref=result_ref,
                )
        return task_id

    def row(self, task_id: str) -> Any:
        with self.db.transaction() as conn:
            return tasks_repo.get(conn, task_id)

    def status(self, task_id: str) -> str:
        return str(self.row(task_id).status.value)

    def get(self, path: str, *, htmx: bool = True) -> Response:
        headers = self.run.htmx() if htmx else self.run.with_token()
        return self.run.client.get(path, headers=headers)

    def post(self, path: str) -> Response:
        return self.run.client.post(path, headers=self.run.action(**{"HX-Request": "true"}))

    def fragment(self, task_id: str) -> "Item":
        response = self.get(f"/tasks/{task_id}")
        assert response.status_code == 200, (response.status_code, response.text[:300])
        return Item(response.text, task_id)


@contextmanager
def serving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Tasks]:
    """A finished setup and the running app with two fictional task types. The runner is
    never woken, so a queued row stays queued (it only ever claims after a notify)."""
    with practising(tmp_path, task_registry=_registry()) as practice:
        monkeypatch.setattr(practice.app.state.runner, "notify", lambda: None)
        yield Tasks(practice)


class Item:
    """One task fragment, read by its stable ids."""

    def __init__(self, html: str, task_id: str) -> None:
        self.html = html
        self.task_id = task_id
        self.elements = outline(html)
        root = by_id(self.elements, f"task-{task_id}")
        assert root is not None, f"a #task-{task_id} element in {html[:300]!r}"
        self.root: Element = root
        self.inside = [e for e in self.elements if e.within(root)]

    def part(self, name: str) -> Element | None:
        return by_id(self.inside, f"task-{self.task_id}-{name}")

    def text(self, name: str) -> str | None:
        part = self.part(name)
        return None if part is None else part.text

    def button(self, action: str) -> Element | None:
        path = f"/tasks/{self.task_id}/{action}"
        found = [e for e in self.inside if e.attrs.get("hx-post") == path]
        assert len(found) <= 1, f"at most one {action} control"
        return found[0] if found else None

    @property
    def actions(self) -> set[str]:
        return {action for action in ("cancel", "retry") if self.button(action) is not None}


def assert_fragment_only(html: str) -> None:
    lowered = html.lower()
    for marker in ("<html", "<body", "<head", "<!doctype"):
        assert marker not in lowered, f"a fragment, never a page ({marker})"


def assert_refresh_wiring(root: Element, url: str) -> None:
    assert root.attrs.get("hx-get") == url
    assert root.attrs.get("hx-trigger") == TRIGGER
    assert root.attrs.get("hx-swap") == "outerHTML"
    for name in root.attrs:
        assert not name.startswith("hx-on"), "no inline handlers (ADR-041)"


def assert_action_button(item: Item, action: str, word: str) -> None:
    button = item.button(action)
    assert button is not None, f"a {word} control"
    assert button.tag == "button"
    assert button.text == word
    assert button.attrs.get("type") == "button"
    assert button.attrs.get("hx-target") == f"#task-{item.task_id}"
    assert button.attrs.get("hx-swap") == "outerHTML"
    assert "disabled" not in button.attrs
    assert button.attrs.get("tabindex") in (None, "0"), "reachable by keyboard"


def panel_items(html: str) -> list[str]:
    elements = outline(html)
    panel = by_id(elements, PANEL_ID)
    assert panel is not None, f"a #{PANEL_ID} element in {html[:300]!r}"
    found = []
    for element in elements:
        match = ITEM_ID.fullmatch(element.attrs.get("id") or "")
        if match and element.within(panel):
            found.append(match.group(1))
    return found


def database_state(data_dir: Path) -> tuple[str, list[str]]:
    """Every table's rows and the files in the data folder."""
    database = data_dir / "app.db"
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        dump = "\n".join(conn.iterdump())
    files = sorted(
        str(path.relative_to(data_dir))
        for path in data_dir.rglob("*")
        if path.is_file() and not path.name.endswith(("-wal", "-shm", ".log"))
    )
    return dump, files


# --- the panel ---------------------------------------------------------------------------------


def test_panel_lists_recent_tasks_with_status_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        seeded = {
            "queued": tasks.add("queued"),
            "running": tasks.add("running", task_type=DRAFT, progress=40),
            "succeeded": tasks.add("succeeded", result_ref="fictional:1"),
            "failed": tasks.add("failed", task_type=DRAFT, error=NetworkUnavailable.kind),
            "cancelled": tasks.add("cancelled"),
            "interrupted": tasks.add("interrupted", task_type=DRAFT),
        }
        response = tasks.get(PANEL)
        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert response.headers["content-type"].startswith("text/html")
        assert_fragment_only(response.text)

        panel = by_id(outline(response.text), PANEL_ID)
        assert panel is not None
        assert_refresh_wiring(panel, PANEL)
        assert by_id(outline(response.text), EMPTY_ID) is None

        # Newest change first: the reverse of the order they were written in.
        assert panel_items(response.text) == list(reversed(seeded.values()))

        labels = {DRAFT: DRAFT_LABEL, ECHO: ECHO_LABEL}
        for status, task_id in seeded.items():
            item = Item(response.text, task_id)
            assert item.text("status") == WORDS[status], status
            assert item.text("label") == labels[tasks.row(task_id).type], status
            expected = {
                "queued": {"cancel"},
                "running": {"cancel"},
                "succeeded": set(),
                "failed": {"retry"},
                "cancelled": {"retry"},
                "interrupted": {"retry"},
            }[status]
            assert item.actions == expected, status
            assert (item.part("result") is not None) == (status == "succeeded"), status
            assert (item.part("reason") is not None) == (status in ("failed", "interrupted"))
        # Raw type names and states are never the visible words.
        for raw in (ECHO, DRAFT, "succeeded"):
            assert raw not in panel.text

        # A later change moves a task to the top.
        with tasks.db.transaction() as conn:
            assert tasks_repo.request_cancel(conn, seeded["running"], tasks._now())
        again = panel_items(tasks.get(PANEL).text)
        assert again[0] == seeded["running"]
        assert sorted(again) == sorted(seeded.values())


def test_panel_empty_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        response = tasks.get(PANEL)
        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert_fragment_only(response.text)
        elements = outline(response.text)
        panel = by_id(elements, PANEL_ID)
        assert panel is not None
        assert_refresh_wiring(panel, PANEL)  # it fills itself when the first task starts
        empty = by_id(elements, EMPTY_ID)
        assert empty is not None and empty.within(panel)
        assert empty.text == MSG_EMPTY
        assert panel_items(response.text) == []
        assert [e for e in elements if e.tag in ("button", "progress")] == []


# --- one task ----------------------------------------------------------------------------------


def test_task_fragment_shows_each_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        queued = tasks.fragment(tasks.add("queued"))
        assert_refresh_wiring(queued.root, f"/tasks/{queued.task_id}")
        assert queued.text("status") == WORDS["queued"]
        assert queued.text("label") == ECHO_LABEL
        assert queued.actions == {"cancel"}
        assert_action_button(queued, "cancel", "Cancel")
        assert queued.part("reason") is None
        assert queued.part("result") is None
        assert_fragment_only(queued.html)
        assert len(outline(queued.html)) == 1 + len(queued.inside), "the root holds it all"

        done = tasks.fragment(tasks.add("succeeded", result_ref="fictional:7"))
        assert done.text("status") == WORDS["succeeded"]
        assert done.actions == set()
        assert done.part("result") is not None, "a place for the result link (M2.20)"
        assert done.part("reason") is None
        assert done.part("progress") is None

        cancelled = tasks.fragment(tasks.add("cancelled"))
        assert cancelled.text("status") == WORDS["cancelled"]
        assert cancelled.actions == {"retry"}
        assert_action_button(cancelled, "retry", "Retry")
        assert cancelled.part("reason") is None, "the user cancelled it: nothing to explain"
        assert cancelled.part("result") is None


def test_running_task_progress_has_accessible_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        item = tasks.fragment(tasks.add("running", task_type=DRAFT, progress=40))
        assert item.text("status") == WORDS["running"]
        assert item.actions == {"cancel"}
        progress = item.part("progress")
        assert progress is not None and progress.tag == "progress"
        assert progress.attrs.get("value") == "40"
        assert progress.attrs.get("max") == "100"
        assert [e for e in item.inside if e.tag == "progress"] == [progress]
        label = (progress.attrs.get("aria-label") or "").strip()
        if not label:
            names = [by_id(item.elements, ref) for ref in _refs(progress, "aria-labelledby")]
            assert names and all(name is not None for name in names), "a labelling element"
            label = " ".join(name.text for name in names if name is not None)
        assert DRAFT_LABEL in label, "named after its task, so two bars read differently"

        fresh = tasks.fragment(tasks.add("running"))
        bar = fresh.part("progress")
        assert bar is not None and bar.attrs.get("value") == "0"


def _refs(element: Element, name: str) -> list[str]:
    return (element.attrs.get(name) or "").split()


def test_running_task_with_cancel_requested_shows_cancelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        item = tasks.fragment(tasks.add("running", cancel_requested=True))
        assert item.text("status") == CANCELLING
        assert item.actions == set(), "already asked to stop; not retryable while it runs"


def test_unregistered_type_shows_neutral_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        assert tasks.practice.app.state.task_registry.get(GONE) is None
        task_id = tasks.add("failed", task_type=GONE, error="UnknownTaskType")
        for html in (tasks.fragment(task_id).html, tasks.get(PANEL).text):
            item = Item(html, task_id)
            assert item.text("label") == NEUTRAL_LABEL
            assert GONE not in html
            assert item.text("reason") == "This task can't run in this version of the app."
            assert item.actions == {"retry"}


# --- failure reasons ---------------------------------------------------------------------------

REASONS = {
    "usage_limit": ("failed", UsageLimitReached.kind, UsageLimitReached.message),
    "network": ("failed", NetworkUnavailable.kind, NetworkUnavailable.message),
    "invalid_output": ("failed", InvalidModelOutput.kind, InvalidModelOutput.message),
    "budget_blocked": ("failed", "BudgetBlocked", MSG_BUDGET),
    "interrupted": ("interrupted", None, MSG_INTERRUPTED),
}


@pytest.mark.parametrize("case", list(REASONS))
def test_failed_task_shows_mapped_reason_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    status, kind, message = REASONS[case]
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add(status, error=kind)
        assert tasks.row(task_id).error == kind
        for html in (tasks.fragment(task_id).html, tasks.get(PANEL).text):
            item = Item(html, task_id)
            assert item.text("status") == WORDS[status]
            assert item.text("reason") == message
            assert item.actions == {"retry"}
            assert_action_button(item, "retry", "Retry")
            assert item.part("progress") is None
            assert item.part("result") is None
            if kind is not None:
                assert kind not in html, "the stored kind is never shown"
            assert "None" not in html


@pytest.mark.parametrize("status", ["failed", "interrupted"])
@pytest.mark.parametrize(
    "stored",
    [HOSTILE, "FictionalKindFromTheFuture", "usagelimitreached", " UsageLimitReached", ""],
    ids=["hostile", "unknown", "wrong-case", "padded", "empty"],
)
def test_stored_error_text_is_never_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: str, status: str
) -> None:
    """Only a known kind maps to a message; anything else in ``tasks.error`` reads as the
    generic message (an interrupted task keeps its own reason)."""
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add(status, error=stored)
        assert tasks.row(task_id).error == stored
        expected = GENERIC if status == "failed" else MSG_INTERRUPTED
        for html in (tasks.fragment(task_id).html, tasks.get(PANEL).text):
            item = Item(html, task_id)
            assert item.text("reason") == expected
            assert item.actions == {"retry"}
            for part in HOSTILE_PARTS:
                assert part not in html
            if stored.strip():
                assert stored.strip() not in html
            assert [e for e in item.elements if e.tag == "script"] == []


def test_failed_task_without_a_kind_shows_generic_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        item = tasks.fragment(tasks.add("failed", error=None))
        assert item.text("reason") == GENERIC
        assert item.actions == {"retry"}


# --- token and origin --------------------------------------------------------------------------


def test_task_fragment_requires_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add("queued")
        client = tasks.run.client
        for path in (PANEL, f"/tasks/{task_id}"):
            for headers in (
                {},
                {"HX-Request": "true"},
                {"origin": ORIGIN},
                {TOKEN_HEADER: "fictional-wrong-token"},
            ):
                refused = client.get(path, headers=headers)
                assert refused.status_code == 403, (path, headers, refused.status_code)
                assert "location" not in refused.headers
                assert "hx-redirect" not in refused.headers
                assert ECHO_LABEL not in refused.text
                assert task_id not in refused.text
            assert client.get(f"{path}?token={tasks.run.token}").status_code == 403
            # The token alone is enough for a GET (no origin needed), htmx or not.
            for headers in (tasks.run.with_token(), tasks.run.htmx()):
                allowed = client.get(path, headers=headers)
                assert allowed.status_code == 200, (path, allowed.status_code)
                assert ECHO_LABEL in allowed.text


@pytest.mark.parametrize("action", ["cancel", "retry"])
@pytest.mark.parametrize("missing", ["token", "origin", "foreign_origin"])
def test_cancel_and_retry_need_token_and_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, missing: str
) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add("queued" if action == "cancel" else "failed", error=None)
        before = tasks.row(task_id)
        headers = {
            "token": {"origin": ORIGIN},
            "origin": tasks.run.with_token(),
            "foreign_origin": tasks.run.with_token(origin="http://fictional.example"),
        }[missing]
        path = f"/tasks/{task_id}/{action}"
        refused = tasks.run.client.post(path, headers={**headers, "HX-Request": "true"})
        assert refused.status_code == 403, refused.status_code
        assert tasks.row(task_id) == before, "a refused request changes nothing"
        assert task_id not in refused.text

        # GET never acts, even with the token and origin.
        got = tasks.run.client.get(path, headers=tasks.run.action())
        assert got.status_code in (404, 405), got.status_code
        assert tasks.row(task_id) == before

        # The same request with both is accepted.
        accepted = tasks.post(path)
        assert accepted.status_code == 200, (accepted.status_code, accepted.text[:300])
        assert tasks.status(task_id) == ("cancelled" if action == "cancel" else "queued")


# --- cancel ------------------------------------------------------------------------------------


def test_cancel_route_returns_cancelled_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-M2.4 (the route's part): a queued task ends cancelled at once."""
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add("queued")
        other = tasks.add("queued")
        response = tasks.post(f"/tasks/{task_id}/cancel")
        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert_fragment_only(response.text)
        item = Item(response.text, task_id)
        assert_refresh_wiring(item.root, f"/tasks/{task_id}")
        assert item.text("status") == WORDS["cancelled"]
        assert item.text("label") == ECHO_LABEL
        assert item.actions == {"retry"}
        assert item.part("reason") is None
        assert item.part("progress") is None
        assert by_id(item.elements, PANEL_ID) is None, "the task's fragment, not the panel"
        assert other not in response.text

        row = tasks.row(task_id)
        assert str(row.status.value) == "cancelled"
        assert row.error is None and row.result_ref is None
        assert tasks.status(other) == "queued"

        # Asking again changes nothing and answers the same fragment.
        again = tasks.post(f"/tasks/{task_id}/cancel")
        assert again.status_code == 200
        assert Item(again.text, task_id).text("status") == WORDS["cancelled"]
        assert tasks.row(task_id) == row


def test_cancel_running_task_shows_cancelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running task only gets the flag (the runner ends it), so it still runs."""
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add("running", progress=20)
        response = tasks.post(f"/tasks/{task_id}/cancel")
        assert response.status_code == 200, (response.status_code, response.text[:300])
        item = Item(response.text, task_id)
        assert item.text("status") == CANCELLING
        assert item.actions == set()
        row = tasks.row(task_id)
        assert str(row.status.value) == "running"
        assert row.cancel_requested is True


@pytest.mark.parametrize("status", ["succeeded", "failed", "interrupted", "cancelled"])
def test_cancel_finished_task_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add(status, error=NetworkUnavailable.kind if status == "failed" else None)
        before = tasks.row(task_id)
        response = tasks.post(f"/tasks/{task_id}/cancel")
        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert Item(response.text, task_id).text("status") == WORDS[status]
        assert tasks.row(task_id) == before


# --- retry -------------------------------------------------------------------------------------


def test_retry_route_requeues_interrupted_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-M2.2 (the route's part): an interrupted task runs again only when asked."""
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add("interrupted", task_type=DRAFT)
        assert tasks.fragment(task_id).text("status") == WORDS["interrupted"]
        assert tasks.status(task_id) == "interrupted", "showing it retries nothing"

        response = tasks.post(f"/tasks/{task_id}/retry")
        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert_fragment_only(response.text)
        item = Item(response.text, task_id)
        assert_refresh_wiring(item.root, f"/tasks/{task_id}")
        assert item.text("status") == WORDS["queued"]
        assert item.text("label") == DRAFT_LABEL
        assert item.actions == {"cancel"}
        assert item.part("reason") is None
        assert MSG_INTERRUPTED not in response.text

        row = tasks.row(task_id)
        assert str(row.status.value) == "queued"
        assert row.attempt == 2
        assert row.error is None
        assert [r.id for r in tasks.practice.app.state.task_service.recent(20)] == [task_id]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_retry_route_requeues_failed_and_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add(status, error=UsageLimitReached.kind if status == "failed" else None)
        response = tasks.post(f"/tasks/{task_id}/retry")
        assert response.status_code == 200, (response.status_code, response.text[:300])
        item = Item(response.text, task_id)
        assert item.text("status") == WORDS["queued"]
        assert item.part("reason") is None
        assert UsageLimitReached.message not in response.text
        assert tasks.row(task_id).attempt == 2


@pytest.mark.parametrize("status", ["queued", "running", "succeeded"])
def test_retry_refused_answers_409_with_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add(status)
        before = tasks.row(task_id)
        response = tasks.post(f"/tasks/{task_id}/retry")
        assert response.status_code == 409, (response.status_code, response.text[:300])
        assert_fragment_only(response.text)
        item = Item(response.text, task_id)
        assert item.text("status") == WORDS[status], "the task as it is now"
        assert item.text("reason") == TaskNotRetryable().user_message
        assert "retry" not in item.actions
        assert f"task is {status}" not in response.text, "the error's detail is for logs"
        assert tasks.row(task_id) == before


def test_retry_with_active_duplicate_answers_that_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        failed = tasks.add("failed", target="fictional-target", error=NetworkUnavailable.kind)
        active = tasks.add("queued", target="fictional-target")
        before = tasks.row(failed)
        response = tasks.post(f"/tasks/{failed}/retry")
        assert response.status_code == 200, (response.status_code, response.text[:300])
        item = Item(response.text, active)
        assert_refresh_wiring(item.root, f"/tasks/{active}")
        assert item.text("status") == WORDS["queued"]
        assert item.actions == {"cancel"}
        assert by_id(item.elements, f"task-{failed}") is None
        assert tasks.row(failed) == before, "the failed attempt stays as it was"
        assert tasks.row(active).attempt == 1


# --- unknown ids -------------------------------------------------------------------------------


def test_unknown_task_404_without_echo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        known = tasks.add("failed", error=NetworkUnavailable.kind)
        bodies = set()
        for missing in MISSING_IDS:
            requests = (
                tasks.get(f"/tasks/{missing}"),
                tasks.get(f"/tasks/{missing}", htmx=False),
                tasks.post(f"/tasks/{missing}/cancel"),
                tasks.post(f"/tasks/{missing}/retry"),
            )
            for response in requests:
                assert response.status_code == 404, (missing, response.status_code)
                body = response.text
                assert_fragment_only(body)
                for shown in (missing, "fictional-missing", "fictional-markup", "t-9999"):
                    assert shown not in body, "the id is never shown back"
                assert "<b>" not in body
                ids = [e.attrs.get("id") or "" for e in outline(body)]
                assert [i for i in ids if i.startswith("task")] == [], "no task is shown"
                bodies.add(body)
        assert len(bodies) == 1, "one fixed answer, whatever was asked"
        (body,) = bodies
        assert body.strip(), "it says the task wasn't found"
        assert "Not Found" not in body, "the app's own words, not the framework's"
        assert len(tasks.practice.app.state.task_service.recent(20)) == 1
        # The routes exist: a known id answers.
        assert tasks.get(f"/tasks/{known}").status_code == 200
        assert tasks.post(f"/tasks/{known}/retry").status_code == 200


# --- no side effects ---------------------------------------------------------------------------


def test_get_task_routes_write_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 2.9 rule 12: showing tasks changes no row, creates no file and starts, retries
    or cancels nothing."""
    with serving(tmp_path, monkeypatch) as tasks:
        ids = [
            tasks.add("queued"),
            tasks.add("running", progress=30),
            tasks.add("running", cancel_requested=True, task_type=DRAFT),
            tasks.add("succeeded", result_ref="fictional:2"),
            tasks.add("failed", error=UsageLimitReached.kind),
            tasks.add("failed", error=HOSTILE),
            tasks.add("cancelled"),
            tasks.add("interrupted"),
            tasks.add("failed", task_type=GONE, error="UnknownTaskType"),
        ]
        data_dir = tasks.practice.app.state.workspace.db.path.parent
        before = database_state(data_dir)
        assert "fictional_echo" in before[0], "the dump holds the seeded rows"

        for _ in range(2):
            for htmx in (True, False):
                assert tasks.get(PANEL, htmx=htmx).status_code == 200
                for task_id in ids:
                    assert tasks.get(f"/tasks/{task_id}", htmx=htmx).status_code == 200
                assert tasks.get("/tasks/t-9999", htmx=htmx).status_code == 404
                head = tasks.run.client.head(PANEL, headers=tasks.run.htmx())
                assert head.status_code in (200, 405)

        assert database_state(data_dir) == before
        assert tasks.practice.fake.requests == []
        assert tasks.practice.usage_rows() == []


# --- fragment or page --------------------------------------------------------------------------


def test_fragment_vs_page_rendering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """These are fragment routes only: the same fragment with or without ``HX-Request``,
    even for a history restore, and never a page without the token."""
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add("running", progress=10)
        client = tasks.run.client
        for path, root_id in ((PANEL, PANEL_ID), (f"/tasks/{task_id}", f"task-{task_id}")):
            htmx = client.get(path, headers=tasks.run.htmx())
            plain = client.get(path, headers=tasks.run.with_token())
            restore = client.get(
                path, headers=tasks.run.htmx(**{"HX-History-Restore-Request": "true"})
            )
            for response in (htmx, plain, restore):
                assert response.status_code == 200, (path, response.status_code)
                assert response.headers["content-type"].startswith("text/html")
                assert_fragment_only(response.text)
                elements = outline(response.text)
                assert elements[0].attrs.get("id") == root_id, "the fragment's root comes first"
                assert all(e.within(elements[0]) for e in elements[1:]), "one root element"
                assert tasks.run.token not in response.text
                assert "hx-headers" not in response.text
            assert plain.text == htmx.text == restore.text

            for headers in ({}, {"HX-Request": "true"}):
                refused = client.get(path, headers=headers)
                assert refused.status_code == 403, (path, refused.status_code)
                assert_fragment_only(refused.text)
                assert root_id not in refused.text


# --- templates ---------------------------------------------------------------------------------


def test_task_templates_have_no_safe_filter_and_never_extend() -> None:
    folder = Path(__file__).resolve().parents[2] / "app" / "templates" / "fragments" / "tasks"
    files = sorted(folder.glob("*.html")) if folder.is_dir() else []
    assert files, "task fragments live under app/templates/fragments/tasks/"
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\|\s*safe\b", text), path.name
        assert "autoescape" not in text, path.name
        assert "{% extends" not in text, f"{path.name}: fragments never extend"
        assert not re.search(r"hx-on[:\-\w]*\s*=", text), f"{path.name}: no inline handlers"
        assert "<script" not in text.lower(), path.name


# --- review follow-ups (M2.19) -----------------------------------------------------------------

MSG_CANCELLED = "Task cancelled."
MSG_CANCELLING = "Cancelling the task."
MSG_REQUEUED = "Task queued again."
MSG_ALREADY_ACTIVE = "This task is already queued or running."
RESYNC = "tasks:resync"
# As sent in a URL: "../", "?", "#", a space, a dot, a colon and an over-long id.
UNSAFE_IDS = (
    "..%2Ft-0001",
    "t-0001%2F..",
    "t-0001%3Fx",
    "t-0001%23x",
    "t%200001",
    "t.0001",
    "t%3A0001",
    "x" * 65,
)


def triggers(response: Response) -> dict[str, Any]:
    """The events a response asks htmx to fire (``HX-Trigger`` as JSON); {} without one."""
    raw = response.headers.get("hx-trigger")
    if raw is None:
        return {}
    found = json.loads(raw)
    assert isinstance(found, dict), raw
    return found


def focused(item: Item) -> list[str | None]:
    return [e.attrs.get("id") for e in item.elements if "data-autofocus" in e.attrs]


class _FixedId:
    def __init__(self, value: str) -> None:
        self._value = value

    def new(self) -> str:
        return self._value


@pytest.mark.parametrize("unsafe", UNSAFE_IDS)
def test_unsafe_ids_answer_404_before_any_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    """Only an id of the safe shape (1 to 64 of A-Z a-z 0-9 _ -) is ever looked up."""
    with serving(tmp_path, monkeypatch) as tasks:
        tasks.add("failed", error=None)  # t-0001 exists, so a sloppy match would find it
        expected = tasks.get("/tasks/t-9999")
        assert expected.status_code == 404
        service = tasks.practice.app.state.task_service

        def never(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("an unsafe id never reaches the task service")

        for name in ("get", "request_cancel", "retry"):
            monkeypatch.setattr(service, name, never)
        for response in (
            tasks.get(f"/tasks/{unsafe}"),
            tasks.get(f"/tasks/{unsafe}", htmx=False),
            tasks.post(f"/tasks/{unsafe}/cancel"),
            tasks.post(f"/tasks/{unsafe}/retry"),
        ):
            assert response.status_code == 404, (unsafe, response.status_code)
            assert response.text == expected.text, "the one fixed answer"
            assert "hx-trigger" not in response.headers


@pytest.mark.parametrize(
    "stored", ["../fictional", "fictional id", "fictional?x", "fictional#x", "y" * 65]
)
def test_panel_skips_row_with_unsafe_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: str
) -> None:
    """A stored id of another shape never becomes an element id or a URL."""
    with serving(tmp_path, monkeypatch) as tasks:
        good = tasks.add("queued")
        tasks.ids = _FixedId(stored)  # type: ignore[assignment]
        assert tasks.add("failed", error=None) == stored
        response = tasks.get(PANEL)
        assert response.status_code == 200
        assert panel_items(response.text) == [good]
        assert "fictional" not in response.text
        assert "y" * 65 not in response.text
        ids = [e.attrs.get("id") or "" for e in outline(response.text)]
        assert [i for i in ids if i.startswith("task-")] == [
            i for i in ids if i.startswith(f"task-{good}")
        ]


def test_action_buttons_have_stable_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        queued = tasks.fragment(tasks.add("queued"))
        cancel = queued.button("cancel")
        assert cancel is not None and cancel.attrs.get("id") == f"task-{queued.task_id}-cancel"
        failed = tasks.fragment(tasks.add("failed", error=None))
        retry = failed.button("retry")
        assert retry is not None and retry.attrs.get("id") == f"task-{failed.task_id}-retry"
        panel = tasks.get(PANEL).text
        for item in (Item(panel, queued.task_id), Item(panel, failed.task_id)):
            for action in item.actions:
                button = item.button(action)
                assert button is not None
                assert button.attrs.get("id") == f"task-{item.task_id}-{action}"


def test_shown_tasks_take_no_focus_and_announce_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh on a task event must never move focus or speak."""
    with serving(tmp_path, monkeypatch) as tasks:
        ids = [tasks.add("queued"), tasks.add("failed", error=None), tasks.add("cancelled")]
        panel = tasks.get(PANEL)
        assert "data-autofocus" not in panel.text
        assert "hx-trigger" not in panel.headers
        for task_id in ids:
            response = tasks.get(f"/tasks/{task_id}")
            assert "data-autofocus" not in response.text
            assert "hx-trigger" not in response.headers


def test_cancel_answer_moves_focus_and_announces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        queued = tasks.add("queued")
        running = tasks.add("running", task_type=DRAFT)
        done = tasks.add("succeeded")
        for task_id, message in ((queued, MSG_CANCELLED), (running, MSG_CANCELLING)):
            response = tasks.post(f"/tasks/{task_id}/cancel")
            assert response.status_code == 200
            item = Item(response.text, task_id)
            assert focused(item) == [f"task-{task_id}-status"], "focus lands on the new status"
            status = item.part("status")
            assert status is not None and status.attrs.get("data-autofocus") == "true"
            assert triggers(response) == {"announce": message}
        # Nothing changed for a finished task: focus still has a place, nothing is claimed.
        response = tasks.post(f"/tasks/{done}/cancel")
        assert focused(Item(response.text, done)) == [f"task-{done}-status"]
        assert triggers(response) == {}


def test_retry_answer_moves_focus_and_announces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add("interrupted")
        response = tasks.post(f"/tasks/{task_id}/retry")
        assert response.status_code == 200
        item = Item(response.text, task_id)
        assert focused(item) == [f"task-{task_id}-status"]
        status = item.part("status")
        assert status is not None and status.attrs.get("data-autofocus") == "true"
        assert triggers(response) == {"announce": MSG_REQUEUED}


@pytest.mark.parametrize("status", ["queued", "running", "succeeded"])
def test_retry_refused_announces_reason_and_resyncs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add(status)
        response = tasks.post(f"/tasks/{task_id}/retry")
        assert response.status_code == 409
        fired = triggers(response)
        assert fired.get("announce") == TaskNotRetryable().user_message
        assert RESYNC in fired, "whatever showed a Retry button was stale: refetch"
        assert sorted(fired) == ["announce", RESYNC]
        assert focused(Item(response.text, task_id)) == [f"task-{task_id}-status"]


def test_retry_with_active_duplicate_resyncs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer replaces the failed task's element with the active task's, which the panel
    may already list: a resync refetches, so no element id stays on the page twice."""
    with serving(tmp_path, monkeypatch) as tasks:
        failed = tasks.add("failed", target="fictional-target", error=NetworkUnavailable.kind)
        active = tasks.add("queued", target="fictional-target")
        response = tasks.post(f"/tasks/{failed}/retry")
        assert response.status_code == 200
        fired = triggers(response)
        assert RESYNC in fired
        assert fired.get("announce") == MSG_ALREADY_ACTIVE
        assert sorted(fired) == ["announce", RESYNC]
        assert focused(Item(response.text, active)) == [f"task-{active}-status"]


def test_no_workspace_shows_empty_panel_and_404s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no workspace open there are no tasks to show or act on."""
    with serving(tmp_path, monkeypatch) as tasks:
        task_id = tasks.add("failed", error=None)
        before = tasks.row(task_id)
        missing = tasks.get("/tasks/t-9999")
        monkeypatch.setattr(tasks.practice.app.state, "workspace", None)

        panel = tasks.get(PANEL)
        assert panel.status_code == 200
        elements = outline(panel.text)
        empty = by_id(elements, EMPTY_ID)
        assert empty is not None and empty.text == MSG_EMPTY
        assert panel_items(panel.text) == []
        for response in (
            tasks.get(f"/tasks/{task_id}"),
            tasks.post(f"/tasks/{task_id}/cancel"),
            tasks.post(f"/tasks/{task_id}/retry"),
        ):
            assert response.status_code == 404, response.status_code
            assert response.text == missing.text
        assert tasks.row(task_id) == before
