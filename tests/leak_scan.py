"""Tools for the automated secret-leak scan (M1.23; AC-M1.4).

``fictional_secret`` builds a key-shaped secret at run time; ``Evidence`` collects every
surface of a run (responses with their headers, raw log records, console output, log files,
database rows, raw file bytes) and ``find_leaks`` looks in it for the whole secret and every
8-character window that reaches into its random part. ``RawLogRecords`` captures log records in
``logging.Logger.handle``, before any handler filter (so before redaction) sees them.
``ScriptedFactory`` wraps the app's wiring so each Claude provider replays a ``Script`` over the
SDK's real client (no CLI process, no network); ``app_session`` runs the app in-process with
logging set up the way the CLI does it.

Every value is fictional; no key-shaped literal is written in the repository.
"""

import contextlib
import logging
import random
import sqlite3
import string
import traceback
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import CLINotFoundError
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx import Response

from app.api.security import PAGE_TAG
from app.config import DATABASE_FILE
from app.domain.settings import AIProviderSettings, claude_auth_mode
from app.logging_config import SDK_REDUCED_ATTR, UVICORN_LOGGERS, configure_logging
from app.main import LOG_FOLDER, create_app
from app.modules.ai import AIProvider, ConnectionResult
from app.modules.ai.providers.claude_sdk import ClaudeSdkProvider
from app.modules.storage import applications_repo, jobs_repo
from app.onboarding.registry import m1_registry
from app.wiring import AI_TEMP_FOLDER, Wiring
from tests.fact_bank import seed_page_ids
from tests.fakes.claude_query import replaying, success_frames
from tests.fakes.clock import FrozenClock
from tests.integration.test_setup_provider import API_KEY_VARIABLE, TOKEN_VARIABLE, data_folder
from tests.integration.test_setup_redirect import BASE_URL, START, Running
from tests.integration.test_setup_reopen_banner import make_config

GUIDE = "/setup/provider"
CONNECTED = "Connected"
WINDOW = 8  # every 8-character piece of a secret's random part is checked
BODY_LENGTH = 48
_BODY_ALPHABET = string.ascii_letters + string.digits
# Built from parts so no key-shaped literal is in the repository (gitleaks stays quiet).
TOKEN_PREFIX = "-".join(["sk", "ant", "oat01", ""])
API_KEY_PREFIX = "-".join(["sk", "ant", "api03", ""])


# --- the secrets ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Secret:
    """A fictional key-shaped secret: a public format prefix and a random body."""

    label: str
    value: str = field(repr=False)  # never shown, even for a live credential
    public_prefix: str

    def needles(self) -> list[str]:
        """The whole value, then every ``WINDOW``-character window that reaches into the
        random body (windows wholly inside the public prefix are the format marker only)."""
        windows = [
            self.value[i : i + WINDOW]
            for i in range(len(self.value) - WINDOW + 1)
            if i + WINDOW > len(self.public_prefix)
        ]
        return [self.value, *dict.fromkeys(windows)]


def fictional_secret(prefix: str, label: str) -> Secret:
    """``prefix`` plus 48 characters from a generator seeded with ``label`` (deterministic,
    unique per label); passes the app's shape check (printable ASCII, 20-1000 characters)."""
    generator = random.Random(f"M1.23 fictional secret {label}")  # noqa: S311 - fictional test data
    body = "".join(generator.choice(_BODY_ALPHABET) for _ in range(BODY_LENGTH))
    return Secret(label=label, value=prefix + body, public_prefix=prefix)


# --- scripted Claude provider ---------------------------------------------------------------


ErrorFactory = Callable[[], BaseException]


@dataclass(frozen=True)
class Script:
    """What the next connection test's Claude provider does: replay ``frames`` over the SDK's
    real client (writing ``stderr`` lines through the provider's stderr callback, then raising
    ``transport_error`` from the transport), or, with ``provider_error``, a stand-in provider
    whose ``test_connection`` raises it. ``failure`` is the failure kind the connection test
    must report (``None``: it must connect)."""

    name: str
    frames: Sequence[Mapping[str, Any]] = ()
    stderr: Sequence[str] = ()
    transport_error: ErrorFactory | None = None
    provider_error: ErrorFactory | None = None
    failure: str | None = None


def connected() -> Script:
    return Script(CONNECTED, frames=success_frames({"ok": True}))


class RaisingProvider:
    """A provider whose connection test raises (the service's ``timeout``/``other`` paths and
    an ``AppError`` carrying text in its detail)."""

    name = "claude"

    def __init__(self, error: ErrorFactory) -> None:
        self._error = error

    async def test_connection(self, model: str) -> ConnectionResult:
        raise self._error()


class ScriptedFactory:
    """The app's wiring, with each Claude provider built from ``script`` (no CLI process, no
    network). The provider keeps wiring's secret store, so it reads the saved credential (or
    the environment one) at call time. Everything else is the wrapped wiring's."""

    def __init__(self, wiring: Wiring) -> None:
        self._wiring = wiring
        self.script = connected()
        self.runs: list[str] = []
        self.credentials_sent: list[str | None] = []  # what the CLI would have received

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wiring, name)

    def ai_provider(self, settings: AIProviderSettings, *, data_dir: Path) -> AIProvider:
        provider = self._wiring.ai_provider(settings, data_dir=data_dir)  # wiring's own checks
        if not isinstance(provider, ClaudeSdkProvider):
            return provider
        script = self.script
        self.runs.append(script.name)
        if script.provider_error is not None:
            return RaisingProvider(script.provider_error)
        mode = claude_auth_mode(settings.kind) if settings.kind is not None else None
        assert mode is not None
        error = script.transport_error() if script.transport_error is not None else None
        query = replaying(script.frames, raise_exc=error, stderr_lines=script.stderr)
        recorded = self.credentials_sent

        def client_factory(options: Any) -> Any:
            env = dict(options.env)
            recorded.append(env.get(TOKEN_VARIABLE) or env.get(API_KEY_VARIABLE) or None)
            return query(options)

        cli = data_dir / "fictional-cli" / "claude"  # a path only; never run
        return ClaudeSdkProvider(
            secrets=self._wiring.secrets,
            mode=mode,
            client_factory=client_factory,
            cli_path=lambda: cli,
            environ={},  # never the developer's environment
            temp_root=data_dir.joinpath(*AI_TEMP_FOLDER),
        )


def error_frames(
    *,
    status: int | None,
    assistant_error: str | None,
    text: str,
    stderr_echo: str | None = None,
) -> list[dict[str, Any]]:
    """A fictional failed run: the assistant turn (with ``assistant_error``) and an error
    result (with ``status``), both carrying ``text``."""
    frames = success_frames(None, model="<synthetic>", assistant_error=assistant_error)
    assistant, result = frames[1], frames[2]
    assistant["message"]["content"] = [{"type": "text", "text": text}]
    result.update(is_error=True, result=text, terminal_reason="api_error")
    result["errors"] = [text] if stderr_echo is None else [text, stderr_echo]
    if status is not None:
        result["api_error_status"] = status
    return frames


def failure_scripts(text: str) -> list[Script]:
    """One script per failure kind the connection test can report (ADR-068 point 5): auth,
    usage_limit, rejected, network, invalid_output, not_installed (provider), then timeout and
    other (service). ``text`` is what the provider's error text says."""
    no_object = success_frames(None)
    no_object[2]["result"] = text
    scripts = [
        Script(
            "auth", error_frames(status=401, assistant_error="authentication_failed", text=text)
        ),
        Script("usage_limit", error_frames(status=429, assistant_error="rate_limit", text=text)),
        Script("rejected", error_frames(status=400, assistant_error="invalid_request", text=text)),
        Script("network", error_frames(status=529, assistant_error="server_error", text=text)),
        Script("invalid_output", no_object),
        Script("not_installed", transport_error=lambda: CLINotFoundError(text)),
        Script("timeout", provider_error=lambda: TimeoutError(text)),
        Script("other", provider_error=lambda: RuntimeError(text)),
    ]
    return [
        Script(s.name, s.frames, s.stderr, s.transport_error, s.provider_error, failure=s.name)
        for s in scripts
    ]


# --- capturing every surface --------------------------------------------------------------


_STANDARD_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))
) | {"message", "asctime", "taskName", SDK_REDUCED_ATTR}


def _record_texts(record: logging.LogRecord) -> list[str]:
    """Everything a handler could write from ``record``, as it was created."""
    texts = [str(record.msg), repr(record.args)]
    with contextlib.suppress(Exception):  # an unformattable record keeps its msg and args
        texts.append(record.getMessage())
    if record.exc_info and record.exc_info[1] is not None:
        texts.append("".join(traceback.format_exception(record.exc_info[1])))
    for value in (record.exc_text, record.stack_info):
        if value:
            texts.append(value)
    for key, value in vars(record).items():
        if key not in _STANDARD_RECORD_ATTRS:
            texts.append(f"{key}={value!r}")
    return texts


class RawLogRecords:
    """Records captured in ``Logger.handle``: after the logger made them, before any handler
    (and so before the redaction filter) saw them. Every record is checked as it is, the
    Claude Agent SDK's included."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []  # (where, text)
        self.sources: list[tuple[str, int]] = []  # (logger name, level) of each record

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = logging.Logger.handle
        entries, sources = self.entries, self.sources

        def handle(logger: logging.Logger, record: logging.LogRecord) -> None:
            where = f"log record {record.name}:{record.levelname} {record.pathname}:{record.lineno}"
            entries.extend((where, text) for text in _record_texts(record))
            sources.append((str(record.name), record.levelno))
            original(logger, record)

        monkeypatch.setattr(logging.Logger, "handle", handle)

    def __repr__(self) -> str:
        # Counts only: the captured texts may hold a secret (a live credential, in M1.25).
        return f"RawLogRecords(<{len(self.sources)} records, {len(self.entries)} texts hidden>)"


@dataclass(repr=False)
class Evidence:
    """Everything one test produced, to be scanned for each secret. Its repr shows counts
    only, so a failing test's locals or assertion never print what it holds."""

    responses: list[tuple[str, Response]] = field(default_factory=list)
    texts: list[tuple[str, str]] = field(default_factory=list)  # (where, text)
    blobs: list[tuple[str, bytes]] = field(default_factory=list)  # (where, raw bytes)

    def __repr__(self) -> str:
        counts = (
            f"{len(self.responses)} responses, {len(self.texts)} texts, {len(self.blobs)} blobs"
        )
        return f"Evidence(<{counts} hidden>)"

    def add_response(self, label: str, response: Response) -> Response:
        self.responses.append((label, response))
        return response

    def response_texts(self) -> Iterator[tuple[str, str]]:
        for label, response in self.responses:
            where = f"{label}: {response.request.method} {response.request.url.path} "
            where += f"-> {response.status_code}"
            yield f"{where} body", response.text
            for name, value in response.headers.multi_items():
                yield f"{where} header {name}", f"{name}: {value}"

    def add_stream(self, label: str, headers: Mapping[str, Sequence[str]], frames: bytes) -> None:
        """An event stream (``GET /events``, M2.18): its response headers and every byte it
        sent. A stream never ends, so it can't be an httpx ``Response``; the test hands over
        what its ASGI driver (``tests/asgi_stream.py``) collected."""
        for name, values in headers.items():
            for value in values:
                self.texts.append((f"{label}: stream header {name}", f"{name}: {value}"))
        self.blobs.append((f"{label}: stream frames", frames))

    def snapshot_files(self, label: str, *folders: Path) -> None:
        """The raw bytes of every file under ``folders`` right now."""
        seen: set[Path] = set()
        for folder in folders:
            if not folder.exists():
                continue
            for path in sorted(folder.rglob("*")):
                if path.is_file() and not path.is_symlink() and path not in seen:
                    seen.add(path)
                    self.blobs.append((f"{label} file {path}", path.read_bytes()))

    def add_database_rows(self, database: Path) -> None:
        """Every cell of every row of every table in ``database`` (read after the app stopped,
        so the WAL has been checkpointed into it)."""
        assert database.is_file(), database
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            tables = [
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            ]
            assert tables, "the database has tables"
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                for number, row in enumerate(connection.execute(f"SELECT * FROM {quoted}")):  # noqa: S608 - table names from sqlite_master
                    for column, cell in enumerate(row):
                        where = f"db row {table}[{number}] column {column}"
                        if isinstance(cell, bytes):
                            self.blobs.append((where, cell))
                        elif cell is not None:
                            self.texts.append((where, str(cell)))
        finally:
            connection.close()


def find_leaks(secret: Secret, evidence: Evidence) -> list[str]:
    """Where the secret, or any checked window of it, shows up."""
    needles = secret.needles()
    encoded = [(needle, needle.encode("utf-8"), needle.encode("utf-16-le")) for needle in needles]
    leaks: list[str] = []
    for where, text in [*evidence.response_texts(), *evidence.texts]:
        hits = [needle for needle in needles if needle in text]
        if hits:
            leaks.append(f"{secret.label} in {where}: {_describe(secret, hits)}")
    for where, blob in evidence.blobs:
        hits = [needle for needle, utf8, utf16 in encoded if utf8 in blob or utf16 in blob]
        if hits:
            leaks.append(f"{secret.label} in {where}: {_describe(secret, hits)}")
    return leaks


def _describe(secret: Secret, hits: list[str]) -> str:
    # Positions only: the report never repeats the (fictional) value itself.
    if secret.value in hits:
        return "the whole value"
    positions = sorted(secret.value.index(hit) for hit in hits)
    return f"{len(hits)} window(s) at offsets {positions}"


def assert_no_leaks(secrets: Iterable[Secret], evidence: Evidence) -> None:
    assert evidence.responses, "the scan saw no responses"
    leaks = [leak for secret in secrets for leak in find_leaks(secret, evidence)]
    assert not leaks, "secret leaked:\n" + "\n".join(leaks)


# --- running the app --------------------------------------------------------------------------


class Session:
    """One app run: every request goes through here, so every response is scanned."""

    def __init__(self, run: Running, evidence: Evidence, label: str) -> None:
        self.run = run
        self._evidence = evidence
        self._label = label

    def get(self, path: str, *, htmx: bool = False) -> Response:
        headers = self.run.htmx() if htmx else {}
        return self._evidence.add_response(self._label, self.run.client.get(path, headers=headers))

    def post(self, path: str, form: Mapping[str, str], *, htmx: bool) -> Response:
        headers = self.run.action(**({"HX-Request": "true"} if htmx else {}))
        response = self.run.client.post(path, data=dict(form), headers=headers)
        return self._evidence.add_response(self._label, response)


@contextmanager
def app_session(
    tmp_path: Path,
    evidence: Evidence,
    label: str,
    *,
    wiring: Wiring | ScriptedFactory,
    setup: bool = False,
) -> Iterator[Session]:
    """The app on the seeded folders, logging like the CLI: JSON log file in ``<data>/logs``
    and the console handler, both filtered with the wiring's redactor, at DEBUG so every
    record is made. The files are snapshotted while the app runs. The root level and the
    uvicorn loggers' handlers and propagation are put back afterwards."""
    root = logging.getLogger()
    level = root.level
    uvicorn = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
        for name in UVICORN_LOGGERS
    }
    logs = configure_logging(data_folder(tmp_path) / LOG_FOLDER, "DEBUG", wiring.redactor)
    try:
        app = create_app(
            make_config(tmp_path, setup=setup), clock=FrozenClock(START), wiring=wiring
        )
        with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
            run = Running(app=app, client=client, hits=Counter())
            yield Session(run, evidence, label)
            evidence.snapshot_files(f"{label} (running)", tmp_path)
    finally:
        logs.close()
        root.setLevel(level)
        for name, (handlers, propagate) in uvicorn.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.propagate = propagate


def visit_every_page(session: Session) -> None:
    """GET every page route the app registers (each guide step for ``/setup/{step_id}``; a
    fictional typed fact for ``/facts/{fact_id}/edit`` and a fictional draft for
    ``/facts/{draft_id}/merge``, both made here by the app's own service), plain and as
    htmx, following one redirect, plus ``/health`` and a missing page."""
    step_ids = [step.id for step in m1_registry().steps]
    fact_ids = seed_page_ids(session.run.app.state.facts)
    job = session.run.app.state.job_intake.add_text(
        "Fictional greenhouse gardener. Maintain imaginary irrigation systems and flowers."
    )
    state = session.run.app.state
    with state.workspace.db.read() as conn:
        stored = jobs_repo.get_job(conn, job.job_id)
        application = applications_repo.get_application_for_job(conn, job.job_id)
    assert stored is not None
    if application is None:
        application = state.applications.apply_to_job(
            job.job_id, expected_job_version=stored.version
        ).application
    from app.modules.storage import sessions_repo

    with state.workspace.db.transaction() as conn:
        if sessions_repo.get_session(conn, "session-leak-page") is None:
            sessions_repo.insert_session(
                conn,
                id="session-leak-page",
                focus_kind="most_useful",
                length=3,
                now="2026-10-30T10:00:00+00:00",
            )
    # M8.3 part A (ADR-124 item 11): a fictional finished interview for the debrief page.
    with state.workspace.db.transaction() as conn:
        if not conn.execute("SELECT 1 FROM events WHERE id = 'leak-debrief-event'").fetchone():
            applications_repo.insert_event(
                conn,
                id="leak-debrief-event",
                application_id=application.id,
                type="interview",
                starts_at="2026-10-29T09:00:00+00:00",
                ends_at="2026-10-29T10:00:00+00:00",
                time_zone="UTC",
                now="2026-10-30T10:00:00+00:00",
            )
    fact_pages = {
        "/applications/{application_id}/debriefs/{event_id}": (
            f"/applications/{application.id}/debriefs/leak-debrief-event"
        ),
        "/sessions/{sid}": "/sessions/session-leak-page",
        "/facts/{fact_id}/edit": f"/facts/{fact_ids['fact_id']}/edit",
        "/facts/{draft_id}/merge": f"/facts/{fact_ids['draft_id']}/merge",
        "/jobs/{job_id}": f"/jobs/{job.job_id}",
        "/companies/{company_id}": "/companies/fictional-company",  # the fixed 404 is enough
        "/applications/{application_id}": f"/applications/{application.id}",
    }
    paths: list[str] = []
    for route in session.run.app.routes:
        if not isinstance(route, APIRoute) or PAGE_TAG not in route.tags:
            continue
        if "GET" not in route.methods:
            continue
        if "{step_id}" in route.path:
            paths.extend(route.path.replace("{step_id}", step) for step in step_ids)
        elif route.path in fact_pages:
            paths.append(fact_pages[route.path])
        else:
            assert "{" not in route.path, f"unknown page parameter in {route.path}"
            paths.append(route.path)
    # The board (M5.4) lists applications: apply for the job so a card is rendered, then read
    # the board's variants. ``/board`` itself is a page without parameters, so it is in ``paths``.
    state = session.run.app.state
    with state.workspace.db.read() as conn:
        stored = jobs_repo.get_job(conn, job.job_id)
    assert stored is not None
    if stored.triage_state != "to_apply":  # a second visit finds it applied for already
        state.applications.apply_to_job(job.job_id, expected_job_version=stored.version)
    paths.extend(
        [
            "/board?view=table&sort=company&dir=desc",
            "/board?closed=1",
            "/board/columns",
            "/board/columns?view=table&sort=role",
        ]
    )
    assert "/board" in paths, paths
    assert "/settings" in paths and "/" in paths and GUIDE in paths, paths
    assert set(fact_pages.values()) <= set(paths), paths
    for path in [*paths, "/health", "/no-such-page-m123"]:
        for htmx in (False, True):
            response = session.get(path, htmx=htmx)
            location = response.headers.get("location")
            if location and location.startswith("/"):
                session.get(location, htmx=htmx)


def scan_after_stop(tmp_path: Path, evidence: Evidence, capsys: pytest.CaptureFixture[str]) -> None:
    """The files and database rows after the app stopped, the console output and the log
    files (which must exist, so the scan saw real logging)."""
    evidence.snapshot_files("after stop", tmp_path)
    evidence.add_database_rows(data_folder(tmp_path) / DATABASE_FILE)
    captured = capsys.readouterr()
    evidence.texts.append(("console stderr", captured.err))
    evidence.texts.append(("console stdout", captured.out))
    log_files = sorted((data_folder(tmp_path) / LOG_FOLDER).glob("*.log*"))
    assert log_files and any(path.stat().st_size for path in log_files), "logs were written"
    for path in log_files:
        evidence.texts.append((f"log file {path.name}", path.read_text(encoding="utf-8")))
