"""The running app for the CV import tests (M3.4): a finished setup with the fake provider
saved, the real task runner, and a spy in front of the provider wiring builds.

The spy never replaces what wiring does: every call is handed to the provider wiring built
(the fake, scripted by wiring for real tasks), so the tests fail while wiring doesn't script
it. A test may hold a call in flight (``hold``) or, for answers the shipped scripts don't
have, give one of its own (``answer``, ``fail``). Rows are read through the app's own
database inside a transaction, files from the temporary data folder. Everything is fictional.
"""

import asyncio
import html
import importlib
import threading
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from app.domain.settings import AIProviderSettings
from app.main import create_app
from app.modules.ai import AIProvider, AIRequest, AIResponse, CancelToken
from app.modules.ai.providers.fake import FakeAIProvider, FakeOutcome, ScriptedResponse
from tests.cv_fixtures import one_space
from tests.fakes.clock import FrozenClock
from tests.integration.test_practice_task import START
from tests.integration.test_setup_provider import make_wiring
from tests.integration.test_setup_redirect import BASE_URL, Running
from tests.integration.test_setup_reopen_banner import finished_guide, make_config
from tests.upload_bodies import CONTENT_TYPE, FieldPart, FilePart, multipart_body
from tests.waiting import wait_until

EXTRACT_CV = "extract_cv"
IMPORT_PATH = "/facts/import"
DEADLINE_S = 20.0  # a parse in a child process takes about 0.6 s; an import parses twice
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")
ACCEPTED = (200, 303)
FACT_TABLES = ("roles", "facts", "fact_versions", "fact_events", "skills", "fact_skills")
HTMX = {"HX-Request": "true"}

_FACTS_SQL = (
    "SELECT f.id, f.status, f.type, f.role_id, f.cv_import_id, v.text, v.situation, v.action, "
    "v.result, v.source, v.original_answer, v.provider, v.model, v.prompt_id, "
    "v.prompt_version, v.needs_check FROM facts AS f "
    "JOIN fact_versions AS v ON v.id = f.current_version_id ORDER BY f.number"
)


def start_path(import_id: str) -> str:
    return f"{IMPORT_PATH}/{import_id}/start"


def remove_path(import_id: str) -> str:
    return f"{IMPORT_PATH}/{import_id}/remove"


def dedupe_target(import_id: str) -> str:
    return f"cv_import:{import_id}"


def said(response: Response) -> str:
    """The answer's text as a reader sees it: entities decoded, whitespace runs as one space."""
    return one_space(html.unescape(response.text))


def message(name: str) -> str:
    """The friendly message (spec 2.11) of the error class ``name``."""
    errors = importlib.import_module("app.domain.errors")
    assert hasattr(errors, name), f"app.domain.errors.{name} doesn't exist yet"
    text: str = getattr(errors, name)().user_message
    return text


class Hold:
    """While ``hold`` is set, every provider call waits here until the test lets it go (or,
    with a ``timeout``, until that passes). Never a fixed sleep."""

    def __init__(self, timeout: float | None = DEADLINE_S) -> None:
        self.hold = False
        self._timeout = timeout
        self._permits = threading.Semaphore(0)

    async def wait(self) -> None:
        if self.hold:
            await asyncio.to_thread(self._permits.acquire, True, self._timeout)

    def open(self) -> None:
        self.hold = False
        for _ in range(50):
            self._permits.release()


class SpyProvider:
    """In front of the provider wiring built: counts and keeps the requests, holds a call
    while the test says so, then lets wiring's provider answer (or the test's own script)."""

    name = FakeAIProvider.name
    capabilities = FakeAIProvider.capabilities

    def __init__(self, hold: Hold) -> None:
        self._hold = hold
        self.inner: AIProvider | None = None
        self._own: FakeAIProvider | None = None
        self.requests: list[AIRequest] = []
        self.entered = 0

    async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
        self.requests.append(request)
        self.entered += 1
        await self._hold.wait()
        provider = self._own if self._own is not None else self.inner
        assert provider is not None, "wiring built no provider"
        return await provider.run(request, cancel)

    async def test_connection(self, model: str) -> Any:
        assert self.inner is not None
        return await self.inner.test_connection(model)

    def known_models(self) -> Any:
        assert self.inner is not None
        return self.inner.known_models()

    def answer(self, data: Mapping[str, Any]) -> None:
        """Every call from now on gets ``data`` (an answer the shipped scripts don't have)."""
        self._script(ScriptedResponse(data=data))

    def fail(self, outcome: FakeOutcome) -> None:
        """Every call from now on fails as ``outcome`` (``usage_limit``, ``network``...)."""
        self._script(ScriptedResponse(failure=outcome))

    def as_wired(self) -> None:
        """Back to the answers of the provider wiring built."""
        self._own = None

    def _script(self, response: ScriptedResponse) -> None:
        own = FakeAIProvider()
        own.script(EXTRACT_CV, response)
        self._own = own


class SpyWiring:
    """The app's wiring, except that the fake it builds is answered through the spy."""

    def __init__(self, wiring: Any, spy: SpyProvider) -> None:
        self._wiring = wiring
        self._spy = spy

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wiring, name)

    def ai_provider(self, settings: AIProviderSettings, *, data_dir: Path) -> Any:
        provider = self._wiring.ai_provider(settings, data_dir=data_dir)
        if not isinstance(provider, FakeAIProvider):
            return provider
        self._spy.inner = provider
        return self._spy


class Importing:
    def __init__(
        self, run: Running, spy: SpyProvider, hold: Hold, clock: FrozenClock, data_dir: Path
    ) -> None:
        self.run = run
        self.spy = spy
        self.hold = hold
        self.clock = clock
        self.data_dir = data_dir

    @property
    def app(self) -> FastAPI:
        return self.run.app

    @property
    def client(self) -> TestClient:
        return self.run.client

    @property
    def imports_dir(self) -> Path:
        return self.data_dir / "imports"

    # --- requests ---------------------------------------------------------------------------

    def post_body(self, body: bytes, *, htmx: bool = False) -> Response:
        headers = self.run.action(**{"content-type": CONTENT_TYPE, **(HTMX if htmx else {})})
        return self.client.post(IMPORT_PATH, content=body, headers=headers)

    def paste(self, text: str, *, htmx: bool = False) -> Response:
        return self.post_body(multipart_body(FieldPart(text)), htmx=htmx)

    def upload(self, filename: str, data: bytes, *, htmx: bool = False) -> Response:
        part = FilePart(filename, data, content_type="application/pdf")
        return self.post_body(multipart_body(part), htmx=htmx)

    def start(self, import_id: str, *, htmx: bool = False, **values: str) -> Response:
        headers = self.run.action(**(HTMX if htmx else {}))
        return self.client.post(start_path(import_id), data=values, headers=headers)

    def remove(self, import_id: str) -> Response:
        return self.client.post(remove_path(import_id), headers=self.run.action())

    # --- what is stored ---------------------------------------------------------------------

    def rows(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        with self.app.state.workspace.db.transaction() as conn:
            cursor = conn.execute(sql, params)
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        with self.app.state.workspace.db.transaction() as conn:
            conn.execute(sql, params)

    def imports(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM cv_imports ORDER BY rowid")

    def only_import(self) -> dict[str, Any]:
        found = self.imports()
        assert len(found) == 1, f"expected one stored import, found {len(found)}"
        return found[0]

    def stored_files(self) -> list[Path]:
        if not self.imports_dir.exists():
            return []
        return sorted(path for path in self.imports_dir.rglob("*") if path.is_file())

    def facts(self) -> list[dict[str, Any]]:
        """Every fact with its current version, in the order they were made."""
        return self.rows(_FACTS_SQL)

    def roles(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM roles ORDER BY rowid")

    def counts(self) -> dict[str, int]:
        return {
            table: self.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"]  # noqa: S608
            for table in FACT_TABLES
        }

    def assert_no_fact_rows(self) -> None:
        assert self.counts() == dict.fromkeys(FACT_TABLES, 0), "nothing was written"

    def assert_nothing_stored(self) -> None:
        """What every refused upload must leave: no file, no row, no task, no provider call."""
        assert self.imports() == [], "no import row"
        assert self.stored_files() == [], "no stored file"
        assert self.tasks() == [], "no task"
        assert self.spy.requests == [], "no provider call"
        self.assert_no_fact_rows()

    def assert_upload_kept(self, import_id: str) -> dict[str, Any]:
        """The import is still there, not done, with its file: it can be started or removed."""
        row = self.only_import()
        assert row["id"] == import_id
        assert row["state"] == "uploaded"
        assert [path.name for path in self.stored_files()] == [row["stored_name"]]
        return row

    # --- tasks ------------------------------------------------------------------------------

    def tasks(self) -> list[Any]:
        rows = self.app.state.task_service.recent(50)
        return [row for row in rows if row.type == EXTRACT_CV]

    def only_task(self) -> Any:
        rows = self.tasks()
        assert len(rows) == 1, [(row.id, str(row.status)) for row in rows]
        return rows[0]

    def status(self, task_id: str) -> str:
        return str(self.app.state.task_service.get(task_id).status.value)

    def wait_terminal(self, task_id: str) -> Any:
        def ended() -> Any:
            row = self.app.state.task_service.get(task_id)
            return row if str(row.status.value) in TERMINAL else None

        return wait_until(ended, timeout=DEADLINE_S, message=f"import task {task_id} ended")

    def wait_entered(self, calls: int) -> None:
        wait_until(
            lambda: self.spy.entered >= calls,
            timeout=DEADLINE_S,
            message=f"{calls} provider call(s) reached the provider",
        )

    def finished(self, response: Response, *, status: str = "succeeded") -> Any:
        """The one import task an accepted upload started, once it has ended as ``status``."""
        assert response.status_code in ACCEPTED, (response.status_code, response.text[:300])
        row = self.wait_terminal(self.only_task().id)
        assert str(row.status.value) == status, (str(row.status.value), row.error)
        return row

    def usage_rows(self) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM usage_records WHERE task_type = ? ORDER BY rowid", (EXTRACT_CV,)
        )


@contextmanager
def importing(
    tmp_path: Path,
    *,
    seed: bool = True,
    provider: AIProviderSettings | None = None,
    environ: dict[str, str] | None = None,
    hold_timeout: float | None = DEADLINE_S,
) -> Iterator[Importing]:
    """The running app on ``tmp_path`` with a finished setup (the fake saved unless
    ``provider`` says otherwise). ``seed=False`` starts on the folders as they are."""
    if seed:
        finished_guide(tmp_path, provider=provider)
    hold = Hold(hold_timeout)
    spy = SpyProvider(hold)
    clock = FrozenClock(START)
    app = create_app(
        make_config(tmp_path),
        clock=clock,
        wiring=SpyWiring(make_wiring(environ), spy),  # type: ignore[arg-type]
    )
    try:
        with TestClient(
            app, base_url=BASE_URL, follow_redirects=False, raise_server_exceptions=False
        ) as client:
            running = Running(app=app, client=client, hits=Counter())
            yield Importing(running, spy, hold, clock, tmp_path / "data")
    finally:
        hold.open()
