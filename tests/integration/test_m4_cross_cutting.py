"""M4.11 cross-cutting checks: AC-M1.4 extended to job text, notes, removal reasons and fetched
URLs (never in logs, task rows, the event stream or error text), using only fictional text.

Two kinds of markers are planted in the real flows (add a job, summarize it and assess its fit
with the fake provider against one confirmed fact, save notes, remove a job with a reason, edit a
company's notes, fetch a link). They may be stored only in the tables of ``JOB_TABLES`` and
``URL_TABLES`` below (an explicit allow-list: a marker in any other table fails) and shown back
to the user who typed them; they must never reach the surfaces that are only ever about tasks
and diagnostics: every log record as it was made (before redaction), the log files, the
console, the rows of the ``tasks``, ``usage_records``, ``provider_usage_snapshots``,
``budget_notices`` and ``settings`` tables, the event stream (headers and frames) and the error
text a person sees when something fails. The failures are forced through a malformed AI answer
and a provider that raises with the text in its detail and in a chained cause, for both
``summarize_job`` and ``assess_fit``, an unexpected exception, a timeout, a refused link, a
redirect to a private address, a refused connection and a fetcher that puts the whole link in
its error.

``test_surface_scan_catches_a_planted_marker`` is the scan's own control: a marker planted on
each surface must be found, so a green result above means "absent", not "not looked at".
"""

import html
import json
import logging
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from app.domain.errors import NetworkUnavailable
from app.domain.redaction import Redactor
from app.domain.similarity import similarity
from app.logging_config import UVICORN_LOGGERS, configure_logging
from app.main import LOG_FOLDER
from app.modules.ai.providers.fake import ScriptedResponse
from app.modules.fetch import FetchedPage, FetchLimits
from app.modules.fetch.providers.fake import FakeJobPageFetcher, FakePage
from app.modules.fetch.providers.guarded import GuardedJobPageFetcher
from app.services.facts import FactInput
from app.wiring import Wiring
from tests.asgi_stream import open_stream
from tests.cv_import_harness import TERMINAL, Importing, importing
from tests.integration.test_setup_reopen_banner import finished_guide
from tests.job_pages import page_bytes, page_entry
from tests.leak_scan import RawLogRecords
from tests.unit.modules.fetch.test_guarded_fetcher import FakeResolver, Recorder, _redirect
from tests.waiting import wait_until

pytestmark = pytest.mark.integration

HTMX = {"HX-Request": "true"}
DEADLINE = 20.0

JOB_MARKER = "FictionalJobMarkerLarkspur"
NOTE_MARKER = "FictionalNoteMarkerTamarind"
REASON_MARKER = "FictionalReasonMarkerQuillback"
COMPANY_MARKER = "FictionalCompanyNoteMarkerBramble"
TEXT_MARKERS = (JOB_MARKER, NOTE_MARKER, REASON_MARKER, COMPANY_MARKER)

PATH_MARKER = "FictionalPathMarkerHeron-7731"
QUERY_MARKER = "FictionalQueryMarkerSedge-9954"

# Where a marker may legitimately be stored (the control that the flows stored it, and that it
# is stored nowhere else). Every table not named here, whatever it holds, must not have it.
JOB_TABLES = frozenset(
    {
        "jobs",
        "job_sources",
        "companies",
        "job_summaries",
        "job_requirements",
        "job_assessments",
        "controlled_values",
        "field_value_queue",
        "jobs_fts",
        "jobs_fts_data",
        "jobs_fts_idx",
        "jobs_fts_content",
        "jobs_fts_docsize",
        "jobs_fts_config",
    }
)
URL_TABLES = frozenset({"jobs", "job_sources", "jobs_fts", "jobs_fts_content", "jobs_fts_data"})
# Tables that are only about tasks, usage and settings: scanned like the logs.
DIAGNOSTIC_TABLES = (
    "tasks",
    "usage_records",
    "provider_usage_snapshots",
    "budget_notices",
    "settings",
)


# --- the surfaces ---------------------------------------------------------------------------


@dataclass(repr=False)
class Surfaces:
    """Every text that is only about tasks and diagnostics. The repr shows counts only."""

    texts: list[tuple[str, str]] = field(default_factory=list)  # (where, text)

    def __repr__(self) -> str:
        return f"Surfaces(<{len(self.texts)} texts hidden>)"

    def add(self, where: str, text: str) -> None:
        self.texts.append((where, text))

    def add_headers(self, where: str, response: httpx.Response) -> None:
        for name, value in response.headers.multi_items():
            self.add(f"{where} header {name}", f"{name}: {value}")

    def add_task_rows(self, run: Importing) -> None:
        for table in DIAGNOSTIC_TABLES:
            for number, row in enumerate(run.rows(f"SELECT * FROM {table} ORDER BY rowid")):  # noqa: S608
                for column, cell in row.items():
                    if cell is not None:
                        self.add(f"{table} row {number} column {column}", str(cell))

    def leaks(self, needles: tuple[str, ...]) -> list[str]:
        return [
            f"{needle!r} in {where}"
            for where, text in self.texts
            for needle in needles
            if needle in text
        ]

    def assert_clean(self, needles: tuple[str, ...]) -> None:
        assert self.texts, "the scan saw nothing"
        found = self.leaks(needles)
        assert not found, "text leaked:\n" + "\n".join(found)


class Watch:
    """A running app with every diagnostic surface collected."""

    def __init__(self, run: Importing, surfaces: Surfaces) -> None:
        self.run = run
        self.surfaces = surfaces

    def post(self, path: str, **values: str) -> httpx.Response:
        headers = self.run.run.action(**HTMX)
        return self.run.client.post(path, data=values, headers=headers)

    def get(self, path: str) -> httpx.Response:
        return self.run.client.get(path, headers=self.run.run.htmx())

    def task_ids(self, task_type: str) -> list[str]:
        return [
            row["id"]
            for row in self.run.rows(
                "SELECT id FROM tasks WHERE type=? ORDER BY rowid", (task_type,)
            )
        ]

    def settle(self, task_type: str, count: int) -> list[dict[str, Any]]:
        """Wait until ``count`` tasks of ``task_type`` exist and every one has ended."""

        def ended() -> list[dict[str, Any]] | None:
            found = self.run.rows("SELECT * FROM tasks WHERE type=? ORDER BY rowid", (task_type,))
            ok = len(found) >= count and all(row["status"] in TERMINAL for row in found)
            return found if ok else None

        return wait_until(ended, timeout=DEADLINE, message=f"{count} {task_type} task(s) ended")

    def settle_all(self) -> None:
        """Wait until there is a task and none is queued or running."""

        def idle() -> bool:
            found = self.run.rows("SELECT status FROM tasks")
            return bool(found) and all(row["status"] in TERMINAL for row in found)

        wait_until(idle, timeout=DEADLINE, message="every task ended")

    def newest_job(self, known: set[str]) -> dict[str, Any]:
        rows = [r for r in self.run.rows("SELECT * FROM jobs") if r["id"] not in known]
        assert len(rows) == 1, [r["id"] for r in rows]
        return rows[0]


@contextmanager
def watched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    fetcher: Any = None,
) -> Iterator[Watch]:
    """The app with the CLI's logging (JSON file in ``<data>/logs`` and the console, at DEBUG),
    log records captured before redaction, and, optionally, ``fetcher`` as the job page fetcher
    wiring builds. After the app stops, the log files and console output join the surfaces."""
    raw = RawLogRecords()
    raw.install(monkeypatch)
    if fetcher is not None:
        monkeypatch.setattr(Wiring, "job_page_fetcher", lambda self: fetcher)
    finished_guide(tmp_path)
    surfaces = Surfaces()
    root = logging.getLogger()
    level = root.level
    uvicorn = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
        for name in UVICORN_LOGGERS
    }
    logs = configure_logging(tmp_path / "data" / LOG_FOLDER, "DEBUG", Redactor())
    try:
        with importing(tmp_path, seed=False) as run:
            yield Watch(run, surfaces)
            surfaces.add_task_rows(run)
    finally:
        logs.close()
        root.setLevel(level)
        for name, (handlers, propagate) in uvicorn.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.propagate = propagate
    surfaces.texts.extend(raw.entries)
    captured = capsys.readouterr()
    surfaces.add("console stderr", captured.err)
    surfaces.add("console stdout", captured.out)
    log_files = sorted((tmp_path / "data" / LOG_FOLDER).glob("*.log*"))
    assert log_files and any(path.stat().st_size for path in log_files), "logs were written"
    for path in log_files:
        surfaces.add(f"log file {path.name}", path.read_text(encoding="utf-8"))
    assert raw.entries, "the scan saw log records"


def tables_containing(run: Importing, needle: str) -> set[str]:
    """Names of the tables that hold ``needle`` in any cell (the control that a flow stored it)."""
    found: set[str] = set()
    with run.app.state.workspace.db.transaction() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            for row in conn.execute(f"SELECT * FROM {quoted}"):  # noqa: S608 - names from sqlite_master
                if any(isinstance(cell, str) and needle in cell for cell in row):
                    found.add(name)
    return found


def assert_stored_only_in(run: Importing, marker: str, allowed: frozenset[str]) -> set[str]:
    """The flows stored ``marker`` (somewhere), and only in the ``allowed`` tables."""
    stored = tables_containing(run, marker)
    assert stored, f"{marker} was not stored by the flows"
    assert stored <= allowed, (marker, sorted(stored - allowed))
    return stored


def open_events(watch: Watch) -> Any:
    portal = watch.run.client.portal
    assert portal is not None
    return portal, portal.wrap_async_context_manager(
        open_stream(watch.run.app, "/events", port=8000, headers=watch.run.run.htmx())
    )


def read_until_final(portal: Any, stream: Any, expected: Mapping[str, str]) -> bytes:
    """Read the stream until every task in ``expected`` was last reported in its final state."""
    seen: dict[str, str] = {}
    frames = b""
    while any(seen.get(task) != status for task, status in expected.items()):
        chunk: bytes = portal.call(stream.next_chunk)
        frames += chunk
        for match in re.finditer(rb"data: (\{[^\n]*\})", chunk):
            payload = json.loads(match.group(1))
            seen[payload["id"]] = payload["status"]
    return frames


def final_states(run: Importing) -> dict[str, str]:
    return {row["id"]: row["status"] for row in run.rows("SELECT id, status FROM tasks")}


# --- the control ------------------------------------------------------------------------------


def test_surface_scan_catches_a_planted_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each surface, planted with a marker, is reported; an unplanted run is clean."""
    planted = "FictionalPlantedMarkerOtter"
    with watched(tmp_path, monkeypatch, capsys) as watch:
        assert watch.get("/health").status_code == 200
        logging.getLogger("jobpipeline.control").info("a control record %s", planted)
        print(planted)
        logging.getLogger("jobpipeline.control").warning("with a trace", exc_info=_raised(planted))
        watch.surfaces.add("control response", planted)
    leaks = watch.surfaces.leaks((planted,))
    where = "\n".join(leaks)
    assert "control response" in where
    assert "log record jobpipeline.control" in where
    assert "console stdout" in where or "console stderr" in where
    assert "log file" in where
    assert "traceback" not in where  # sanity: reports are positions, never a dump
    assert watch.surfaces.leaks(("FictionalNeverPlantedMarkerWren",)) == []
    # A planted marker in a task column is found as well.
    surfaces = Surfaces()
    surfaces.add("tasks row 0 column error", f"Failed: {planted}")
    assert surfaces.leaks((planted,)) == [f"{planted!r} in tasks row 0 column error"]


def _raised(text: str) -> tuple[Any, Any, Any]:
    try:
        raise RuntimeError(text)
    except RuntimeError as error:
        return (type(error), error, error.__traceback__)


# --- job text, notes, reasons -----------------------------------------------------------------


POSTINGS = {
    "greenhouse": (
        "Seedling technician needed to tend tomato benches, adjust misting valves, log humidity "
        "readings and teach weekend volunteers about compost."
    ),
    "lighthouse": (
        "Keeper wanted for the harbour beacon: polish the great lens, inspect the stairwell, "
        "record passing vessels and repaint the railing before the winter storms."
    ),
    "beekeeper": (
        "Apiary assistant to inspect hive frames, extract honey, label jars, carry colonies to "
        "the orchards and explain pollination to visiting schoolchildren."
    ),
    "cartographer": (
        "Mapmaker to digitise coastlines, verify contour lines, draft legends and plot hiking "
        "routes for the regional atlas."
    ),
    "falconer": (
        "Handler to train hawks, clean the mews, schedule flying displays and advise vineyards "
        "on keeping starlings away from the grapes."
    ),
    "orchid": (
        "Grower to pollinate orchids by hand, record bloom dates, repot seedlings and pack "
        "delicate stems for the weekend flower market."
    ),
    "pilot": (
        "Ferry pilot to steer the river barge at dawn, inspect the mooring ropes, announce "
        "stops and keep the passenger deck dry."
    ),
}


def distinct_text(topic: str, extra: str = "") -> str:
    """A fictional posting that no other test posting resembles (no accidental duplicate)."""
    return f"Fictional {topic} wanted at Orchard Test Works. {JOB_MARKER} {POSTINGS[topic]} {extra}"


def test_the_postings_are_clearly_distinct() -> None:
    """Well below the duplicate threshold, so no flow merges two of them."""
    topics = list(POSTINGS)
    for number, first in enumerate(topics):
        for second in topics[number + 1 :]:
            score = similarity(distinct_text(first), distinct_text(second))
            assert score < 0.6, (first, second, score)


def test_job_text_notes_and_reasons_never_in_logs_task_rows_or_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with watched(tmp_path, monkeypatch, capsys) as watch:
        run = watch.run
        portal, opened = open_events(watch)
        with opened as stream:
            assert stream.status == 200
            frames = [portal.call(stream.next_chunk)]

            # 1. One confirmed fact, so the fit task runs after a summary. Add by pasted
            #    text; the fake provider summarizes it, then assesses the fit.
            run.app.state.facts.create(FactInput(text="Worked with Python for three years."))
            added = watch.post("/inbox/intake", text=distinct_text("greenhouse"))
            assert added.status_code == 200
            watch.settle("summarize_job", 1)
            fits = watch.settle("assess_fit", 1)
            assert fits[0]["status"] == "succeeded", fits[0]["status"]
            watch.settle_all()
            job = run.rows("SELECT id, version FROM jobs")[0]
            company = run.rows("SELECT id, version FROM companies")[0]

            # 2. Notes on the job, notes on the company, then removal with the "other" text.
            saved = watch.post(
                f"/inbox/jobs/{job['id']}/edit",
                version=str(job["version"]),
                notes=f"Call back on Monday. {NOTE_MARKER}",
            )
            assert saved.status_code == 200, saved.text[:300]
            noted = watch.post(
                f"/companies/{company['id']}/edit",
                version=str(company["version"]),
                notes=f"Friendly team. {COMPANY_MARKER}",
            )
            assert noted.status_code == 200, noted.text[:300]
            version = run.rows("SELECT version FROM jobs WHERE id=?", (job["id"],))[0]["version"]
            reason = (REASON_MARKER + " " + "x" * 500)[:500]
            assert len(reason) == 500 and REASON_MARKER in reason
            removed = watch.post(
                f"/inbox/jobs/{job['id']}/triage",
                version=str(version),
                action="remove",
                reason="other",
                text=reason,
            )
            assert removed.status_code == 200, removed.text[:300]
            assert run.rows("SELECT triage_state FROM jobs")[0]["triage_state"] == "removed"

            # 3. Failures: a malformed AI answer, then a provider that raises with the text in its
            #    detail and chained cause, an unexpected exception and a timeout.
            known = {job["id"]}
            original = run.spy.run
            failures: list[tuple[str, str]] = []

            def add_failing(topic: str, extra: str = "") -> str:
                before = len(watch.task_ids("summarize_job"))
                response = watch.post("/inbox/intake", text=distinct_text(topic, extra))
                assert response.status_code == 200
                watch.surfaces.add_headers("failing intake", response)
                new = watch.newest_job(known)
                known.add(new["id"])
                tasks = watch.settle("summarize_job", before + 1)
                assert tasks[-1]["status"] == "failed", tasks[-1]["status"]
                failures.append((topic, new["id"]))
                return str(new["id"])

            add_failing("lighthouse", "fake-job-malformed")

            async def network(request: object, cancel: object) -> object:
                raise NetworkUnavailable(detail=JOB_MARKER) from RuntimeError(NOTE_MARKER)

            async def unexpected(request: object, cancel: object) -> object:
                raise RuntimeError(f"unexpected {JOB_MARKER} {REASON_MARKER}")

            async def timeout(request: object, cancel: object) -> object:
                raise TimeoutError(f"timeout {JOB_MARKER} {COMPANY_MARKER}")

            for topic, behaviour in (
                ("beekeeper", network),
                ("cartographer", unexpected),
                ("falconer", timeout),
            ):
                monkeypatch.setattr(run.spy, "run", behaviour)
                add_failing(topic)

            # 3b. The same for the fit task: the summary succeeds, then the fit answer is
            #     malformed (with the text in it), then the provider raises with the posting
            #     text in its detail and in a chained cause.
            fit_failures: list[str] = []
            monkeypatch.setattr(run.spy, "run", original)

            def add_failing_fit(topic: str) -> None:
                before = len(watch.task_ids("assess_fit"))
                response = watch.post("/inbox/intake", text=distinct_text(topic))
                assert response.status_code == 200
                watch.surfaces.add_headers("failing fit intake", response)
                new = watch.newest_job(known)
                known.add(new["id"])
                tasks = watch.settle("assess_fit", before + 1)
                assert tasks[-1]["status"] == "failed", tasks[-1]["status"]
                fit_failures.append(str(new["id"]))

            run.spy.script("assess_fit", ScriptedResponse(raw_text=f"not json {JOB_MARKER} {{"))
            add_failing_fit("orchid")

            async def fit_provider_fails(request: Any, cancel: object) -> object:
                if request.task != "assess_fit":
                    return await original(request, cancel)
                raise NetworkUnavailable(detail=f"{JOB_MARKER} {NOTE_MARKER}") from RuntimeError(
                    f"{REASON_MARKER} {COMPANY_MARKER}"
                )

            monkeypatch.setattr(run.spy, "run", fit_provider_fails)
            add_failing_fit("pilot")
            fit_errors = {
                row["error"] for row in run.rows("SELECT error FROM tasks WHERE type='assess_fit'")
            }
            assert "InvalidModelOutput" in fit_errors, fit_errors

            # 4. What a person sees about these failures: the job's summary area, the card,
            #    the tasks panel and indicator, Settings usage.
            for _topic, job_id in failures:
                summary = watch.get(f"/inbox/jobs/{job_id}/summary")
                assert summary.status_code == 200
                # A failed job's summary area shows a fixed sentence about the failure and none
                # of the posting's text, so its whole body is scanned.
                assert 'role="alert"' in summary.text
                watch.surfaces.add("failed job summary area", summary.text)
                watch.surfaces.add_headers("failed job summary area", summary)
                # The card legitimately shows the job's own title (made from its text), so
                # only its headers and its status line are scanned.
                card = watch.get(f"/inbox/jobs/{job_id}/card")
                assert card.status_code == 200
                watch.surfaces.add_headers("failed job card", card)
                for status_line in re.findall(r'role="(?:status|alert)"[^>]*>([^<]*)<', card.text):
                    watch.surfaces.add("failed job card status line", html.unescape(status_line))
            for job_id in fit_failures:
                # The fit area lists the posting's requirements (its own text), so only its
                # status lines and headers are scanned.
                fit = watch.get(f"/inbox/jobs/{job_id}/fit")
                assert fit.status_code == 200
                lines = re.findall(r'role="(?:status|alert)"[^>]*>([^<]*)<', fit.text)
                assert lines, "a failed fit shows a status line"
                for line in lines:
                    watch.surfaces.add("failed fit area status line", html.unescape(line))
                watch.surfaces.add_headers("failed fit area", fit)
            for path in ("/tasks/panel", "/tasks/indicator", "/settings/usage"):
                shown = watch.get(path)
                assert shown.status_code == 200, path
                watch.surfaces.add(f"{path} body", shown.text)
                watch.surfaces.add_headers(path, shown)

            # 5. The stream, through the last failure.
            terminal = {task: status for task, status in final_states(run).items()}
            assert set(terminal.values()) <= {"succeeded", "failed"}
            assert list(terminal.values()).count("failed") == 6
            frames.append(read_until_final(portal, stream, terminal))
            watch.surfaces.add("event stream headers", repr(stream.headers))
            watch.surfaces.add("event stream frames", b"".join(frames).decode("utf-8", "replace"))

        # Control: the flows stored the text, and only in the tables that hold job data.
        for marker in TEXT_MARKERS:
            allowed = JOB_TABLES | {"job_removals"} if marker == REASON_MARKER else JOB_TABLES
            assert_stored_only_in(run, marker, allowed)
        assert "job_removals" in tables_containing(run, REASON_MARKER)
        assert "jobs" in tables_containing(run, JOB_MARKER)
        assert "companies" in tables_containing(run, COMPANY_MARKER)
        failed_errors = {
            r["error"] for r in run.rows("SELECT error FROM tasks WHERE error IS NOT NULL")
        }
        assert "InvalidModelOutput" in failed_errors, failed_errors
    watch.surfaces.assert_clean(TEXT_MARKERS)


# --- fetched URLs -----------------------------------------------------------------------------


def link(host: str, *, scheme: str = "https") -> str:
    return f"{scheme}://{host}/postings/{PATH_MARKER}/gardener?ref={QUERY_MARKER}&utm_source=zz"


OK_HOST = "ok.leakcheck.example.test"
REDIRECT_HOST = "go.leakcheck.example.test"
DOWN_HOST = "down.leakcheck.example.test"
BUSY_HOST = "busy.leakcheck.example.test"
LEAKY_HOST = "leaky.leakcheck.example.test"
BOOM_HOST = "boom.leakcheck.example.test"
PUBLIC_ADDRESS = "93.184.215.14"


class LeakyFetcher:
    """A fetcher whose errors carry the whole link, in the detail and in the chained cause."""

    def __init__(self, error: Callable[[str], BaseException]) -> None:
        self._error = error

    async def fetch(self, url: str, limits: FetchLimits) -> FetchedPage:
        raise self._error(url)


def connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)


class ByHost:
    """Sends each fictional host to its own scripted fetcher; anything else (literal addresses)
    goes to the real guarded fetcher, which refuses them before any connection."""

    def __init__(self) -> None:
        page = FakePage(content_type=page_entry("plain")["content_type"], body=page_bytes("plain"))
        resolver = FakeResolver(
            {host: [PUBLIC_ADDRESS] for host in (REDIRECT_HOST, DOWN_HOST, BUSY_HOST)}
        )
        self.recorders = {
            REDIRECT_HOST: Recorder(
                lambda request: _redirect(f"http://127.0.0.1/{PATH_MARKER}?ref={QUERY_MARKER}")
            ),
            DOWN_HOST: Recorder(connect_error),
            BUSY_HOST: Recorder(lambda request: httpx.Response(503, content=b"busy")),
        }
        self._by_host: dict[str, Any] = {
            OK_HOST: FakeJobPageFetcher({link(OK_HOST): page}),
            LEAKY_HOST: LeakyFetcher(_leaky),
            BOOM_HOST: LeakyFetcher(lambda url: RuntimeError(f"boom {url}")),
        }
        for host, recorder in self.recorders.items():
            self._by_host[host] = GuardedJobPageFetcher(
                resolver=resolver, transport=recorder.transport
            )
        self._default = GuardedJobPageFetcher()

    async def fetch(self, url: str, limits: FetchLimits) -> FetchedPage:
        host = urlsplit(url).hostname or ""
        return await self._by_host.get(host, self._default).fetch(url, limits)


def _leaky(url: str) -> BaseException:
    try:
        raise RuntimeError(url)
    except RuntimeError as cause:
        error = NetworkUnavailable(detail=url)
        error.__cause__ = cause
        return error


def intake_error_text(response: httpx.Response) -> str:
    """What a person reads as the error: the alert paragraph of the form (the form also echoes
    the link the person typed, which is theirs), or the whole body of a plain answer."""
    found = re.search(r'<p id="job-intake-error"[^>]*>(.*?)</p>', response.text, re.DOTALL)
    if response.status_code == 422 and found:
        return html.unescape(found.group(1))
    return response.text


def test_fetched_url_never_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fetcher = ByHost()
    needles = (PATH_MARKER, QUERY_MARKER, link(OK_HOST), f"ref={QUERY_MARKER}")
    scenarios = [
        ("fetched", link(OK_HOST)),
        ("redirect to a private address", link(REDIRECT_HOST)),
        ("connection refused", link(DOWN_HOST)),
        ("service unavailable", link(BUSY_HOST)),
        ("fetcher error carrying the link", link(LEAKY_HOST)),
        ("unexpected fetcher error", link(BOOM_HOST)),
        ("literal private address", link("127.0.0.1", scheme="http")),
        ("literal link-local address", link("169.254.169.254", scheme="http")),
        ("unsupported scheme", link("ftp.leakcheck.example.test", scheme="ftp")),
        ("local file", f"file:///{PATH_MARKER}?ref={QUERY_MARKER}"),
    ]
    with watched(tmp_path, monkeypatch, capsys, fetcher=fetcher) as watch:
        run = watch.run
        portal, opened = open_events(watch)
        with opened as stream:
            frames = [portal.call(stream.next_chunk)]
            outcomes: dict[str, int] = {}
            for name, url in scenarios:
                response = watch.post("/inbox/intake", url=url)
                outcomes[name] = response.status_code
                watch.surfaces.add(f"{name}: error text", intake_error_text(response))
                watch.surfaces.add_headers(f"{name}: response", response)
            # The link was fetched and saved once; the others were refused or failed with the
            # same friendly answer (422 asks for the pasted text), except a fetcher bug, which
            # is a plain server error (500) whose text is scanned like the rest.
            assert outcomes == {
                "fetched": 200,
                "redirect to a private address": 422,
                "connection refused": 422,
                "service unavailable": 422,
                "fetcher error carrying the link": 422,
                "unexpected fetcher error": 500,
                "literal private address": 422,
                "literal link-local address": 422,
                "unsupported scheme": 422,
                "local file": 422,
            }, outcomes
            assert run.rows("SELECT count(*) AS n FROM jobs")[0]["n"] == 1
            watch.settle("summarize_job", 1)
            watch.settle_all()
            for path in ("/tasks/panel", "/tasks/indicator", "/settings/usage"):
                shown = watch.get(path)
                watch.surfaces.add(f"{path} body", shown.text)
                watch.surfaces.add_headers(path, shown)
            terminal = final_states(run)
            frames.append(read_until_final(portal, stream, terminal))
            watch.surfaces.add("event stream headers", repr(stream.headers))
            watch.surfaces.add("event stream frames", b"".join(frames).decode("utf-8", "replace"))
        # Control: the saved job keeps the link as its source, and nothing else does.
        assert "job_sources" in assert_stored_only_in(run, PATH_MARKER, URL_TABLES)
        assert_stored_only_in(run, QUERY_MARKER, URL_TABLES)
        assert fetcher.recorders[REDIRECT_HOST].requests, "the redirect was really requested"
    watch.surfaces.assert_clean(needles)
