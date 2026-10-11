"""AC-M1.4 extended to tasks, usage rows, budgets, the event stream and logs (M2.26; spec 2.9,
2.11, 2.13, 3.3; ADR-077).

The real app runs in-process on this test's event loop (its lifespan, the task runner and the
``/events`` stream), with real SQLite under ``tmp_path`` and logging set up the way the CLI
does it (``configure_logging`` into ``<data>/logs`` at DEBUG with the wiring's redactor).
Requests go through httpx's ASGI transport; the stream is read with ``tests/asgi_stream.py``.

What is planted (all fictional, built at run time by ``tests/leak_scan.fictional_secret``):

- a provider credential saved in the ``SecretStore`` (the in-memory test keyring);
- a key-shaped string that is saved nowhere, and two "personal" markers (a name-like and an
  address-like string), all of them in everything the fake provider says: the raw text of
  every answer, the detail of every provider error, the cause chained to it, the message
  of an unexpected exception and the window of a usage snapshot it tries to report
  (``TalkativeFake``; that snapshot is refused where snapshots are validated, and a
  well-formed one is reported next, so ``provider_usage_snapshots`` has a row to scan);
- two markers in the stored target and input of a task that a "crash" leaves running: the
  restarted app shows it as interrupted. Those two are stored on purpose (the scan's
  control finds them in the database), so they are looked for in what the app shows and
  sends: every response and the event stream's headers and frames.

Practice tasks then run to success, to every failure the practice form offers, to an
unexpected handler failure, to a cancel and to a retry, with a budget set so that a warning
dialog is shown and confirmed and the blocking point is reached, while ``/events`` is open.
Every surface is scanned with the M1.23 scanner (the whole value and every 8-character window
of it): each response (panel, indicator, single task, Settings, the usage panel, the dialog,
the blocked answer), the stream's headers and bytes, log records before redaction, the console,
the log files, every row of every table (``tasks``, ``usage_records``,
``provider_usage_snapshots``, ``budget_notices``, ``settings``, ...) and the raw bytes of
every file under the test folder.

``tasks.error`` must hold nothing but an error kind from the fixed set (the ``kind`` of an
``AppError`` class), and log records about tasks must be made of ids, types, states and kinds
only: every argument of a record from the task, usage and AI loggers is a number or one short
token without spaces, and its message is one of the constant format strings written in the
app's source (``log_formats``), so a message built at run time (an f-string, a ``%`` or
``+`` expression, ``str.format``) can't carry text past the arguments rule.

No network, no sleeps (the fake's delay is a gate the test opens; a task's end is awaited on
the stream), ``FrozenClock``.
"""

import ast
import asyncio
import copy
import logging
import re
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

import app as app_package
from app.api.security import TOKEN_HEADER
from app.config import DATABASE_FILE
from app.domain.budgets import Budget
from app.domain.errors import (
    AppError,
    InvalidModelOutput,
    NetworkUnavailable,
    TaskFailed,
    UsageLimitReached,
)
from app.domain.settings import AIProviderSettings
from app.domain.tasks import TaskStatus
from app.logging_config import UVICORN_LOGGERS, configure_logging
from app.main import LOG_FOLDER, create_app
from app.modules.ai import (
    AIProvider,
    AIRequest,
    AIResponse,
    ProviderUsageSnapshot,
    report_usage,
)
from app.modules.ai.providers.fake import FakeAIProvider
from app.modules.secrets import CLAUDE_CODE_OAUTH_TOKEN
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.services.tasks import TaskService
from app.wiring import Wiring
from tests.asgi_stream import CLIENT_PORT, LOOPBACK, app_lifespan, open_stream
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.fakes.keyring_backends import MemoryKeyring
from tests.integration.test_budget_warning_dialog import (
    BUDGETS_PATH,
    CONTINUE,
    budget_fields,
    control,
    submitted_by,
    the_dialog,
)
from tests.integration.test_practice_task import (
    FAKE_SERVICE,
    MSG_STARTED,
    PRACTICE,
    PRACTICE_PATH,
    SETTINGS,
    START,
)
from tests.integration.test_setup_provider import data_folder, make_wiring
from tests.integration.test_setup_redirect import BASE_URL, ORIGIN, PORT
from tests.integration.test_setup_reopen_banner import finished_guide, make_config
from tests.integration.test_task_events import EVENTS, Reader
from tests.leak_scan import (
    API_KEY_PREFIX,
    TOKEN_PREFIX,
    Evidence,
    RawLogRecords,
    Secret,
    assert_no_leaks,
    fictional_secret,
    find_leaks,
    scan_after_stop,
)

pytestmark = pytest.mark.integration

DEADLINE = 5.0
PANEL = "/tasks/panel"
INDICATOR = "/tasks/indicator"
USAGE_PANEL = "/settings/usage"
TOOLS = "/settings/testing-tools"
ERROR_KINDS_SEEN = {
    NetworkUnavailable.kind,
    InvalidModelOutput.kind,
    UsageLimitReached.kind,
    TaskFailed.kind,
}
# Loggers that write about tasks, usage and provider calls.
TASK_LOGGERS = ("app.tasks", "app.services", "app.modules.ai", "app.api.tasks", "app.api.events")
# An id, a task type, a state, an error kind or an exception's class name: one short token.
TOKEN = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")
UNEXPECTED = "FictionalProviderCrash"
APP_DIR = Path(app_package.__file__).resolve().parent
LOG_METHODS = frozenset({"debug", "info", "warning", "error", "critical", "exception", "log"})
SNAPSHOT_WINDOW = "month"


# --- what is planted -----------------------------------------------------------------------


@dataclass(frozen=True)
class Planted:
    credential: Secret  # saved in the SecretStore
    unsaved_key: Secret  # key-shaped, saved nowhere: the redactor doesn't know it
    person: Secret  # a "personal" marker in provider text
    address: Secret  # another one

    @property
    def all(self) -> tuple[Secret, ...]:
        return (self.credential, self.unsaved_key, self.person, self.address)

    @property
    def provider_text(self) -> str:
        return (
            f"Fictional provider text: {self.person.value} of {self.address.value} "
            f"sent {self.credential.value} and {self.unsaved_key.value}"
        )


def plant(label: str) -> Planted:
    return Planted(
        credential=fictional_secret(TOKEN_PREFIX, f"{label} saved credential"),
        unsaved_key=fictional_secret(API_KEY_PREFIX, f"{label} unsaved key"),
        person=fictional_secret("Fictional-Person-", f"{label} personal marker"),
        address=fictional_secret("12-Fictional-Street-", f"{label} address marker"),
    )


class FictionalProviderCrash(Exception):
    """Not an ``AppError``: what a provider bug would raise."""


class Gate:
    """The fake's ``sleep``, on the test's loop: while ``hold`` is set a call waits until the
    test opens the gate (never longer than the deadline)."""

    def __init__(self) -> None:
        self.hold = False
        self.entered = asyncio.Event()
        self._open = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.entered.set()
        if self.hold:
            await asyncio.wait_for(self._open.wait(), DEADLINE)

    def open(self) -> None:
        self.hold = False
        self._open.set()


class TalkativeFake(FakeAIProvider):
    """The fake provider, except that everything it says carries ``text``: each answer's raw
    text, each provider error's detail and chained cause, with ``crash`` set an unexpected
    exception's message, and the window of a usage snapshot it tries to report with every
    call. That snapshot never gets built (``refused`` counts them); a well-formed one is
    reported instead (``reported``), so the snapshots table is not empty when scanned."""

    def __init__(self, text: str, gate: Gate, clock: FrozenClock) -> None:
        super().__init__(sleep=gate.sleep)
        self._text = text
        self._clock = clock
        self.crash = False
        self.said = 0
        self.refused = 0
        self.reported = 0

    def _snapshot(self, window: str) -> ProviderUsageSnapshot:
        return ProviderUsageSnapshot(
            service=FAKE_SERVICE,
            window=window,
            unit="requests",
            used=1,  # never above the app's own count, so no budget decision changes
            limit=None,
            utilization=None,
            resets_at=None,
            observed_at=self._clock.now(),
        )

    def _report(self, request: AIRequest) -> None:
        try:
            report_usage(request.on_usage, self._snapshot(self._text))
        except ValueError:
            self.refused += 1  # provider text is no window: refused at the boundary
        report_usage(request.on_usage, self._snapshot(SNAPSHOT_WINDOW))
        self.reported += 1

    async def run(self, request: AIRequest, cancel: Any) -> AIResponse:
        try:
            response = await super().run(request, cancel)
        except AppError as error:
            if type(error) not in (NetworkUnavailable, InvalidModelOutput, UsageLimitReached):
                raise  # the cancel signal and anything else, untouched
            self.said += 1
            self._report(request)
            raise type(error)(detail=self._text) from RuntimeError(self._text)
        self.said += 1
        self._report(request)
        if self.crash:
            raise FictionalProviderCrash(self._text)
        return AIResponse(
            raw_text=f"{response.raw_text}\n{self._text}",
            data=response.data,
            model=response.model,
            usage=response.usage,
        )


class TalkativeWiring:
    """The app's wiring, except that the fake it builds is the test's talkative one."""

    def __init__(self, wiring: Wiring, fake: TalkativeFake) -> None:
        self._wiring = wiring
        self._fake = fake

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wiring, name)

    def ai_provider(self, settings: AIProviderSettings, *, data_dir: Path) -> AIProvider:
        provider = self._wiring.ai_provider(settings, data_dir=data_dir)
        return self._fake if isinstance(provider, FakeAIProvider) else provider


# --- log capture ----------------------------------------------------------------------------


class Records:
    """Every log record as its logger made it (``Logger.handle``, before any filter): a copy
    taken then, because the redacting filter later replaces a record's message with the
    formatted text and drops its arguments."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.records: list[logging.LogRecord] = []
        original = logging.Logger.handle
        records = self.records

        def handle(logger: logging.Logger, record: logging.LogRecord) -> None:
            records.append(copy.copy(record))
            original(logger, record)

        monkeypatch.setattr(logging.Logger, "handle", handle)

    def about_tasks(self) -> list[logging.LogRecord]:
        return [r for r in self.records if r.name.startswith(TASK_LOGGERS)]


@contextmanager
def cli_logging(tmp_path: Path, wiring: Any) -> Iterator[None]:
    """Logging as the CLI sets it up, at DEBUG; the root level and the uvicorn loggers are
    put back afterwards (as ``tests/leak_scan.app_session`` does)."""
    root = logging.getLogger()
    level = root.level
    uvicorn = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
        for name in UVICORN_LOGGERS
    }
    logs = configure_logging(data_folder(tmp_path) / LOG_FOLDER, "DEBUG", wiring.redactor)
    try:
        yield
    finally:
        logs.close()
        root.setLevel(level)
        for name, (handlers, propagate) in uvicorn.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.propagate = propagate


# --- the running app ------------------------------------------------------------------------


@dataclass
class Run:
    app: FastAPI
    client: httpx.AsyncClient
    reader: Reader
    stream: Any
    fake: TalkativeFake
    gate: Gate
    clock: FrozenClock
    evidence: Evidence
    label: str

    def _headers(self, *, htmx: bool, action: bool) -> dict[str, str]:
        headers = {TOKEN_HEADER: str(self.app.state.launch_token)}
        if htmx:
            headers["HX-Request"] = "true"
        if action:
            headers["origin"] = ORIGIN
        return headers

    async def get(self, path: str, *, htmx: bool = True) -> httpx.Response:
        response = await self.client.get(path, headers=self._headers(htmx=htmx, action=False))
        return self.evidence.add_response(self.label, response)

    async def post(self, path: str, values: dict[str, str], *, htmx: bool = True) -> httpx.Response:
        headers = self._headers(htmx=htmx, action=True)
        response = await self.client.post(path, data=values, headers=headers)
        return self.evidence.add_response(self.label, response)

    async def look_everywhere(self) -> None:
        """Every place that shows tasks, usage or budgets, as fragments and as pages."""
        task_ids = [row.id for row in await self.rows()]
        for path in (PANEL, INDICATOR, USAGE_PANEL, TOOLS, *(f"/tasks/{i}" for i in task_ids)):
            response = await self.get(path)
            assert response.status_code == 200, (path, response.status_code)
        for path in (SETTINGS, "/"):
            for htmx in (False, True):
                response = await self.get(path, htmx=htmx)
                assert response.status_code == 200, (path, response.status_code)

    async def rows(self) -> list[Any]:
        return list(await asyncio.to_thread(self.app.state.task_service.recent, 50))

    async def start(self, **changes: str) -> httpx.Response:
        values = {"duration": "quick", "outcome": "ok", "tier": "fast", **changes}
        return await self.post(PRACTICE_PATH, values)

    async def started(self, **changes: str) -> str:
        """Start one practice task and return its id."""
        before = {row.id for row in await self.rows()}
        response = await self.start(**changes)
        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert MSG_STARTED in response.text, response.text[:300]
        return await self.new_task(before)

    async def new_task(self, before: set[str]) -> str:
        new = [row.id for row in await self.rows() if row.id not in before]
        assert len(new) == 1, f"one task was enqueued, found {len(new)}"
        return str(new[0])

    async def ended(self, task_id: str, status: str) -> Any:
        """Wait on the stream for the task to reach ``status``; return its row."""
        await self.reader.saw(task_id, status)
        row = await asyncio.to_thread(self.app.state.task_service.get, task_id)
        assert str(row.status.value) == status, (row.status, row.error)
        return row

    async def used(self) -> int:
        status = await asyncio.to_thread(self.app.state.usage.status, FAKE_SERVICE)
        return int(status.local_count)


@asynccontextmanager
async def serving(
    tmp_path: Path, planted: Planted, evidence: Evidence, label: str
) -> AsyncIterator[Run]:
    """The app on the seeded folders, with CLI logging, one open ``/events`` stream and a
    client. The stream's headers and bytes and the files as they are while the app runs go
    into ``evidence`` at the end."""
    gate = Gate()
    clock = FrozenClock(START)
    fake = TalkativeFake(planted.provider_text, gate, clock)
    base = make_wiring()
    base.secrets.set(CLAUDE_CODE_OAUTH_TOKEN, planted.credential.value)
    wiring = TalkativeWiring(base, fake)
    with cli_logging(tmp_path, wiring):
        app = create_app(make_config(tmp_path), clock=clock, wiring=wiring)  # type: ignore[arg-type]
        transport = httpx.ASGITransport(app=app, client=(LOOPBACK, CLIENT_PORT))
        try:
            async with (
                app_lifespan(app),
                httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client,
                open_stream(
                    app, EVENTS, port=PORT, headers={TOKEN_HEADER: app.state.launch_token}
                ) as stream,
            ):
                reader = await Reader(stream).opened()
                try:
                    yield Run(app, client, reader, stream, fake, gate, clock, evidence, label)
                finally:
                    gate.open()
                    evidence.add_stream(label, stream.headers, reader.received)
                    evidence.snapshot_files(f"{label} (running)", tmp_path)
        finally:
            gate.open()


# --- the flows -------------------------------------------------------------------------------


async def run_every_outcome(run: Run) -> dict[str, str]:
    """Success, each failure the form offers, an unexpected handler failure, a cancel and a
    retry. Returns the task id of each."""
    ids: dict[str, str] = {}
    ids["ok"] = await run.started()
    row = await run.ended(ids["ok"], "succeeded")
    assert row.error is None

    for outcome, kind in (
        ("network", NetworkUnavailable.kind),
        ("invalid_output", InvalidModelOutput.kind),
        ("usage_limit", UsageLimitReached.kind),
    ):
        ids[outcome] = await run.started(outcome=outcome)
        row = await run.ended(ids[outcome], "failed")
        assert row.error == kind, "the stored error is the kind, nothing else"
        await run.look_everywhere()

    run.fake.crash = True
    ids["crash"] = await run.started()
    row = await run.ended(ids["crash"], "failed")
    run.fake.crash = False
    assert row.error == TaskFailed.kind

    run.gate.hold = True
    run.gate.entered.clear()
    ids["cancel"] = await run.started(duration="slow")
    await asyncio.wait_for(run.gate.entered.wait(), DEADLINE)
    await run.look_everywhere()  # a running task, with its progress
    cancelled = await run.post(f"/tasks/{ids['cancel']}/cancel", {})
    assert cancelled.status_code == 200, cancelled.status_code
    run.gate.open()
    row = await run.ended(ids["cancel"], "cancelled")
    assert row.error is None

    before = len(run.reader.statuses(ids["network"]))
    retried = await run.post(f"/tasks/{ids['network']}/retry", {})
    assert retried.status_code == 200, retried.status_code
    await run.reader.until(
        "the retry failing again",
        lambda: run.reader.statuses(ids["network"])[before:].count("failed") == 1,
    )
    await run.look_everywhere()
    return ids


async def warn_then_block(run: Run) -> None:
    """A budget saved through Settings so that the next start crosses the first warning: the
    dialog is shown and confirmed, the task's five requests reach the blocking point and the
    next start is refused."""
    count = await run.used()
    assert count > 0, "the earlier tasks were counted"
    budget = Budget(
        allowance=count + 20,
        first_warning=count + 1,
        second_warning=count + 3,
        block_at=count + 5,
        reset_day=12,
    )
    version = (await asyncio.to_thread(run.app.state.usage.load_budgets)).version
    values = {"service": FAKE_SERVICE, "version": str(version), **budget_fields(budget)}
    saved = await run.post(BUDGETS_PATH, values)
    assert saved.status_code == 200, (saved.status_code, saved.text[:300])
    await run.look_everywhere()

    before = {row.id for row in await run.rows()}
    asked = await run.start()
    elements, dialog = the_dialog(asked)  # type: ignore[arg-type]
    assert {row.id for row in await run.rows()} == before, "showing the dialog starts nothing"
    path, form = submitted_by(elements, control(elements, dialog, CONTINUE))
    confirmed = await run.post(path, form)
    assert confirmed.status_code == 200, (confirmed.status_code, confirmed.text[:300])
    task_id = await run.new_task(before)
    await run.ended(task_id, "succeeded")
    assert await run.used() == budget.block_at, "the task's requests reached the blocking point"

    for htmx in (True, False):
        values = {"duration": "quick", "outcome": "ok", "tier": "fast"}
        blocked = await run.post(PRACTICE_PATH, values, htmx=htmx)
        assert blocked.status_code == 409, blocked.status_code
    assert {row.id for row in await run.rows()} == before | {task_id}, "blocked: nothing started"
    await run.look_everywhere()


def stored(tmp_path: Path, sql: str) -> list[tuple[Any, ...]]:
    database = data_folder(tmp_path) / DATABASE_FILE
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return list(connection.execute(sql))
    finally:
        connection.close()


def error_kinds() -> set[str]:
    """The fixed set: the ``kind`` of every ``AppError`` class."""
    kinds: set[str] = set()
    pending = [AppError]
    while pending:
        cls = pending.pop()
        kinds.add(cls.kind)
        pending.extend(cls.__subclasses__())
    return kinds


def shown_or_sent(evidence: Evidence) -> Evidence:
    """What the app showed or sent: every response (body and headers) and the event stream's
    headers and frames. Not the files, the database rows or the logs."""
    shown = Evidence()
    shown.responses.extend(evidence.responses)
    shown.texts.extend(entry for entry in evidence.texts if ": stream header " in entry[0])
    shown.blobs.extend(entry for entry in evidence.blobs if entry[0].endswith(": stream frames"))
    return shown


def logged(evidence: Evidence) -> Evidence:
    """The console and the log files."""
    logs = Evidence()
    logs.texts.extend(entry for entry in evidence.texts if entry[0].startswith(("console", "log")))
    return logs


def assert_markers_not_shown(markers: tuple[Secret, ...], evidence: Evidence) -> None:
    """A task's stored target and input never reach a response, a stream frame or a log."""
    shown = shown_or_sent(evidence)
    assert shown.responses, "the scan saw responses"
    assert any(blob for _where, blob in shown.blobs), "the scan saw stream frames"
    assert shown.texts, "the scan saw the stream's headers"
    leaks = [
        leak
        for marker in markers
        for where in (shown, logged(evidence))
        for leak in find_leaks(marker, where)
    ]
    assert leaks == [], "a task's target or input was shown, sent or logged:\n" + "\n".join(leaks)


def log_formats() -> frozenset[str]:
    """Every constant string the app's source passes as the message of a logging call
    (``_log.warning("...", ...)``, ``logger.log(level, "...", ...)``). A message built at
    run time is not a constant, so it is not in this set."""
    formats: set[str] = set()
    for path in sorted(APP_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in LOG_METHODS:
                continue
            position = 1 if node.func.attr == "log" else 0
            if len(node.args) <= position:
                continue
            message = node.args[position]
            if isinstance(message, ast.Constant) and isinstance(message.value, str):
                formats.add(message.value)
    return frozenset(formats)


def assert_scanner_sees(planted: Planted) -> None:
    """The scan's own control: the planted text is found when a surface holds it."""
    control_evidence = Evidence()
    control_evidence.texts.append(("control", planted.provider_text))
    for secret in planted.all:
        assert find_leaks(secret, control_evidence), f"{secret.label}: the scanner finds it"


# --- AC-M1.4, extended ---------------------------------------------------------------------------


async def test_ac_m1_4_no_secret_in_task_usage_rows_or_events(
    tmp_path: Path,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    planted = plant("M2.26 leak scan")
    assert_scanner_sees(planted)
    in_target = fictional_secret("Fictional-Target-", "M2.26 marker in an interrupted target")
    in_input = fictional_secret("Fictional-Input-", "M2.26 marker in an interrupted input")
    raw_records = RawLogRecords()
    raw_records.install(monkeypatch)
    finished_guide(tmp_path)
    evidence = Evidence()

    async with serving(tmp_path, planted, evidence, "tasks and budgets") as run:
        await run.look_everywhere()
        ids = await run_every_outcome(run)
        await warn_then_block(run)
        said, refused, reported = run.fake.said, run.fake.refused, run.fake.reported
        frames = run.reader.received
    assert said >= 12, f"control: the provider said the planted text {said} times"
    assert refused == said, "every snapshot with provider text for a window was refused"
    assert reported == said, "control: a well-formed snapshot was reported with every call"
    assert frames.count(b"event: task-updated") >= 3 * len(ids), "the scan saw real frames"

    # A "crash" leaves a task running; its stored target and input carry markers.
    crashed = seed_running_task(tmp_path, in_target.value, in_input.value)

    # After a restart the same rows are shown again (nothing was only held in memory), and
    # the task the crash left running is shown as interrupted.
    async with serving(tmp_path, planted, evidence, "restarted") as run:
        row = await asyncio.to_thread(run.app.state.task_service.get, crashed)
        assert row.status == TaskStatus.INTERRUPTED
        assert row.error is None
        await run.look_everywhere()  # the panel, the indicator, the task itself, the pages
        panel = await run.get(PANEL)
        assert f'id="task-{crashed}"' in panel.text, "control: the interrupted task is shown"
        assert "Interrupted" in panel.text

    # What is stored: a kind from the fixed set, never text.
    errors = stored(tmp_path, "SELECT status, error FROM tasks")
    assert {error for status, error in errors if status == "failed"} == ERROR_KINDS_SEEN
    assert {error for status, error in errors if status != "failed"} == {None}
    assert "interrupted" in {status for status, _error in errors}
    assert error_kinds() >= ERROR_KINDS_SEEN
    assert stored(tmp_path, "SELECT COUNT(*) FROM usage_records")[0][0] >= said
    assert stored(tmp_path, "SELECT COUNT(*) FROM budget_notices")[0][0] >= 1, (
        "the confirmed warning was stored, so the scan saw a budget notice row"
    )
    snapshots = stored(tmp_path, "SELECT service, window, unit FROM provider_usage_snapshots")
    assert snapshots == [(FAKE_SERVICE, SNAPSHOT_WINDOW, "requests")], (
        "the reported snapshot was stored (latest per window), so the scan saw a snapshot row; "
        "nothing with another window got in"
    )

    scan_after_stop(tmp_path, evidence, capsys)
    evidence.texts.extend(raw_records.entries)
    assert raw_records.entries, "log records were captured before redaction"
    assert_no_leaks(planted.all, evidence)

    # The interrupted task's target and input are stored (the scan's control: found in the
    # database), but nothing the app shows or sends repeats them, and no log does.
    for marker in (in_target, in_input):
        assert any(" in db row tasks[" in leak for leak in find_leaks(marker, evidence)), (
            f"control: {marker.label} is stored with the task"
        )
    assert_markers_not_shown((in_target, in_input), evidence)


# --- logs name ids and kinds only --------------------------------------------------------------


def seed_running_task(tmp_path: Path, target: str, input_ref: str) -> str:
    """A task left ``running``, as a killed process leaves it (``test_crash_recovery.py``)."""
    folder = data_folder(tmp_path)
    clock = FrozenClock(START)
    with Database.open(folder / DATABASE_FILE) as db:
        apply_migrations(db, MIGRATIONS_DIR, folder / "backups", clock)
        service = TaskService(db, clock, SequentialIdFactory("crashed"), lambda _change: None)
        task_id = service.enqueue(PRACTICE, target, input_ref=input_ref).id
        claimed = service.claim_next()
        assert claimed is not None and claimed.id == task_id
    return str(task_id)


async def test_task_logs_name_ids_and_kinds_only(
    tmp_path: Path,
    memory_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Records written while tasks start, finish, fail, are cancelled, retried and recovered
    after a crash: no provider text, no target and no input (beyond ids), and every argument
    is a number or one short token (an id, a type, a state, a kind or a class name)."""
    planted = plant("M2.26 task logs")
    in_target = fictional_secret("Fictional-Target-", "M2.26 marker in a task target")
    in_input = fictional_secret("Fictional-Input-", "M2.26 marker in an input ref")
    records = Records(monkeypatch)
    raw_records = RawLogRecords()
    raw_records.install(monkeypatch)
    finished_guide(tmp_path)
    evidence = Evidence()

    async with serving(tmp_path, planted, evidence, "task logs") as run:
        ids = await run_every_outcome(run)
        # A task whose stored target and input are free text (queued behind the form's back).
        ref = await asyncio.to_thread(
            run.app.state.task_service.enqueue,
            PRACTICE,
            in_target.value,
            input_ref=in_input.value,
        )
        row = await run.ended(ref.id, "failed")
        assert row.error == TaskFailed.kind
        await run.look_everywhere()
    crashed = seed_running_task(tmp_path, f"{in_target.value}-2", in_input.value)

    # The restart recovers the task the "crash" left running.
    async with serving(tmp_path, planted, evidence, "after the crash") as run:
        row = await asyncio.to_thread(run.app.state.task_service.get, crashed)
        assert row.status == TaskStatus.INTERRUPTED
        await run.look_everywhere()

    about = records.about_tasks()
    messages = [record.getMessage() for record in about]
    assert any(ids["crash"] in message and UNEXPECTED in message for message in messages), (
        "control: the unexpected failure was logged with the task id and the exception's "
        f"class name; task records: {messages}"
    )
    assert any("nterrupted" in message for message in messages), "control: the recovery was logged"

    formats = log_formats()
    assert len(formats) >= 20, "control: the source's log messages were collected"
    assert "Interrupted %d tasks left running" in formats, "control: a known message"
    assert not any(ids["crash"] in message for message in formats)

    assert any(record.args for record in about), (
        "control: the records still have their arguments (they were copied before any filter)"
    )
    for record in about:
        where = f"{record.name}:{record.lineno} {record.msg!r}"
        # The message itself is a constant from the source: nothing was put into it at run
        # time (an f-string or a formatted string would pass the arguments rule below).
        assert isinstance(record.msg, str), f"{where}: a message that is not a string"
        assert record.msg in formats, f"{where}: a message that is not a constant in the source"
        args = record.args if isinstance(record.args, tuple) else (record.args,)
        for arg in args:
            if arg is None or isinstance(arg, (int, float)):
                continue
            assert isinstance(arg, str), f"{where}: an argument of type {type(arg).__name__}"
            assert TOKEN.fullmatch(arg), f"{where}: an argument that is not an id or a kind"
        assert record.exc_info is None, f"{where}: a traceback could carry provider text"
        assert not record.stack_info, where

    # And nothing planted is in any record, the console or the log files.
    scan_after_stop(tmp_path, evidence, capsys)
    in_logs = logged(evidence)
    in_logs.texts.extend(raw_records.entries)
    assert raw_records.entries, "log records were captured before redaction"
    leaks = [
        leak
        for secret in (*planted.all, in_target, in_input)
        for leak in find_leaks(secret, in_logs)
    ]
    assert leaks == [], "task logs hold more than ids and kinds:\n" + "\n".join(leaks)

    # Nor are the stored target and input in any response or stream frame (they are in the
    # database, which is where they belong).
    for marker in (in_target, in_input):
        assert any(" in db row tasks[" in leak for leak in find_leaks(marker, evidence)), (
            f"control: {marker.label} is stored with the task"
        )
    assert_markers_not_shown((in_target, in_input), evidence)
