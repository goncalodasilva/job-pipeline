"""Module interface types from spec 2.4 (task M0.8, ADR-026).

Interface types only: protocols and dataclasses for ai, search, calendar, secrets and fetch.
Implementations, fakes and contract suites arrive in the milestone that needs them.
"""

import dataclasses
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.modules.ai import (
    AICapabilities,
    AIRequest,
    AIResponse,
    ConnectionResult,
    ModelChoice,
    TokenUsage,
)
from app.modules.calendar import CalendarFile, EventData, ExternalRef
from app.modules.fetch import FetchedPage, FetchLimits
from app.modules.search import SearchResult

REPO_ROOT = Path(__file__).resolve().parents[3]

START = datetime(2026, 3, 10, 14, 0, tzinfo=UTC)
END = START + timedelta(hours=1)


def _request() -> AIRequest:
    return AIRequest(
        task="summarize_job",
        prompt_id="summarize_job",
        prompt_version="1",
        system="You summarize job postings.",
        user='<untrusted_document source="job_posting" id="J1">Fictional role</untrusted_document>',
        output_schema={"type": "object"},
        model="fake-fast",
    )


def _event(**overrides: object) -> EventData:
    values: dict[str, object] = {
        "uid": "event-0001",
        "title": "Interview at Example Widgets Ltd",
        "starts_at": START,
        "ends_at": END,
        "time_zone": "Europe/Lisbon",
    }
    values.update(overrides)
    return EventData(**values)  # type: ignore[arg-type]


def test_ai_request_defaults_no_web_search_and_no_hook() -> None:
    # Spec 2.9.7 and 3.3: plain AI tasks run with no tools allowed.
    request = _request()

    assert request.allow_web_search is False
    assert request.on_tool_use is None


def test_ai_capabilities_default_false() -> None:
    capabilities = AICapabilities()

    assert capabilities.web_search is False
    assert capabilities.speech_to_text is False
    assert capabilities.text_to_speech is False
    assert capabilities.reports_token_usage is False


def test_optional_fields_have_documented_defaults() -> None:
    assert ConnectionResult(ok=True, message="Connected.").model is None
    assert _event().details == ""


FROZEN_INSTANCES = [
    AICapabilities(),
    TokenUsage(input_tokens=10, output_tokens=5),
    ModelChoice(id="fake-fast", label="Fast"),
    ConnectionResult(ok=False, message="Could not connect."),
    _request(),
    AIResponse(raw_text="{}", data={}, model="fake-fast", usage=None),
    SearchResult(title="Fictional role", url="https://jobs.example.com/1", snippet="A role."),
    _event(),
    CalendarFile(filename="interview.ics", content=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"),
    ExternalRef(provider="ics", external_id="evt-0001"),
    FetchLimits(),
    FetchedPage(
        requested_url="https://jobs.example.com/1",
        final_url="https://jobs.example.com/1",
        status=200,
        content_type="text/html",
        text="<p>Fictional role</p>",
    ),
]


@pytest.mark.parametrize("instance", FROZEN_INSTANCES, ids=lambda obj: type(obj).__name__)
def test_dataclasses_are_frozen(instance: object) -> None:
    assert dataclasses.is_dataclass(instance)
    for field in dataclasses.fields(instance):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, field.name, None)


def test_fetch_limits_match_spec_2_9() -> None:
    limits = FetchLimits()

    assert limits.max_bytes == 5 * 1024 * 1024
    assert limits.timeout_seconds == 15.0
    assert limits.max_redirects == 5


def test_event_data_requires_aware_times_and_order() -> None:
    naive_start = START.replace(tzinfo=None)
    naive_end = END.replace(tzinfo=None)

    with pytest.raises(ValueError):
        _event(starts_at=naive_start)
    with pytest.raises(ValueError):
        _event(ends_at=naive_end)
    with pytest.raises(ValueError):
        _event(starts_at=END, ends_at=START)

    instant = _event(starts_at=START, ends_at=START)
    assert instant.starts_at == instant.ends_at


def test_calendar_file_defaults_to_ics_media_type() -> None:
    calendar_file = CalendarFile(filename="interview.ics", content=b"BEGIN:VCALENDAR\r\n")

    assert calendar_file.media_type == "text/calendar"


CONFORMANCE = """
from collections.abc import Mapping
from typing import Any, Literal

from app.modules.ai import (
    AICapabilities,
    AIProvider,
    AIRequest,
    AIResponse,
    CancelToken,
    ConnectionResult,
    ModelChoice,
    TokenUsage,
)
from app.modules.calendar import CalendarFile, CalendarProvider, EventData, ExternalRef
from app.modules.fetch import FetchedPage, FetchLimits, JobPageFetcher
from app.modules.search import SearchProvider, SearchResult
from app.modules.secrets import SecretStore


class DummyCancel:
    @property
    def cancelled(self) -> bool:
        return False


class DummyAI:
    name: str = "dummy"
    capabilities: AICapabilities = AICapabilities(reports_token_usage=True)

    async def test_connection(self, model: str) -> ConnectionResult:
        return ConnectionResult(ok=True, message="Connected.", model=model)

    def known_models(self) -> list[ModelChoice]:
        return [ModelChoice(id="fake-fast", label="Fast")]

    async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
        data: Mapping[str, Any] = {"ok": not cancel.cancelled}
        return AIResponse(
            raw_text="{}",
            data=data,
            model=request.model,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )


class DummySearch:
    name: str = "dummy"
    mode: Literal["delegated", "standalone"] = "standalone"
    storage_allowed: bool | None = None
    reports_usage: bool = False

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        return [SearchResult(title=query, url="https://example.com", snippet="")][:limit]


class DummyCalendar:
    name: str = "ics"
    supports_sync: bool = False

    def export_event(self, event: EventData) -> CalendarFile:
        return CalendarFile(filename="event.ics", content=event.title.encode())

    async def sync_event(self, event: EventData) -> ExternalRef:
        return ExternalRef(provider=self.name, external_id=event.title)


class DummySecrets:
    available: bool = True

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def set(self, key: str, value: str) -> None:
        self._values[key] = value

    def delete(self, key: str) -> None:
        self._values.pop(key, None)


class DummyFetcher:
    async def fetch(self, url: str, limits: FetchLimits) -> FetchedPage:
        return FetchedPage(
            requested_url=url,
            final_url=url,
            status=200,
            content_type="text/plain",
            text="x" * min(limits.max_bytes, 1),
        )


async def allow_tool(tool_name: str) -> bool:
    return tool_name == "web_search"


request = AIRequest(
    task="answer_web_question",
    prompt_id="answer_web_question",
    prompt_version="1",
    system="system",
    user="user",
    output_schema={"type": "object"},
    model="fake-fast",
    allow_web_search=True,
    on_tool_use=allow_tool,
)

cancel: CancelToken = DummyCancel()
ai: AIProvider = DummyAI()
search: SearchProvider = DummySearch()
calendar: CalendarProvider = DummyCalendar()
secrets: SecretStore = DummySecrets()
fetcher: JobPageFetcher = DummyFetcher()


class DelegatedSearch:
    # A provider that is always delegated and never stores: narrower types must be accepted.
    name = "claude-web-search"
    mode: Literal["delegated"] = "delegated"
    storage_allowed: bool = False
    reports_usage = False

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        return []


class ComputedSecrets(DummySecrets):
    @property
    def available(self) -> bool:  # type: ignore[override]
        return False


delegated: SearchProvider = DelegatedSearch()
computed: SecretStore = ComputedSecrets()
"""

SEARCH_MODE_WRONG_TYPE = """
from app.modules.search import SearchProvider, SearchResult


class NumberedMode:
    name = "x"
    mode = 3
    storage_allowed = None
    reports_usage = False

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        return []


search: SearchProvider = NumberedMode()
"""

AI_RUN_NOT_ASYNC = """
from app.modules.ai import (
    AICapabilities,
    AIProvider,
    AIRequest,
    AIResponse,
    CancelToken,
    ConnectionResult,
    ModelChoice,
)


class SyncRunAI:
    name: str = "dummy"
    capabilities: AICapabilities = AICapabilities()

    async def test_connection(self, model: str) -> ConnectionResult:
        return ConnectionResult(ok=True, message="Connected.")

    def known_models(self) -> list[ModelChoice]:
        return []

    def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
        return AIResponse(raw_text="", data={}, model=request.model, usage=None)


ai: AIProvider = SyncRunAI()
"""

STORE_SET_MISSING_PARAM = """
from app.modules.secrets import SecretStore


class ShortSetSecrets:
    available: bool = True

    def get(self, key: str) -> str | None:
        return None

    def set(self, key: str) -> None:
        return None

    def delete(self, key: str) -> None:
        return None


secrets: SecretStore = ShortSetSecrets()
"""


def _mypy(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    sample = tmp_path / "conformance.py"
    sample.write_text(source, encoding="utf-8")
    env = {**os.environ, "MYPYPATH": str(REPO_ROOT)}
    # Fixed argv to the venv's own interpreter; no shell, no untrusted input.
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            str(sample),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_protocol_conformance(tmp_path: Path) -> None:
    result = _mypy(tmp_path, CONFORMANCE)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("source", "protocol"),
    [
        pytest.param(AI_RUN_NOT_ASYNC, "AIProvider", id="ai_run_not_async"),
        pytest.param(STORE_SET_MISSING_PARAM, "SecretStore", id="secret_set_missing_value"),
        pytest.param(SEARCH_MODE_WRONG_TYPE, "SearchProvider", id="search_mode_wrong_type"),
    ],
)
def test_protocol_conformance_rejects_mismatches(
    tmp_path: Path, source: str, protocol: str
) -> None:
    result = _mypy(tmp_path, source)

    assert result.returncode != 0, result.stdout + result.stderr
    assert protocol in result.stdout
    assert "Incompatible types in assignment" in result.stdout


VENDOR_SDKS = ["claude_agent_sdk", "keyring", "httpx"]

IMPORT_PROBE = f"""
import json
import sys

before = {{name for name in {VENDOR_SDKS!r} if name in sys.modules}}
from app.modules.ai import AIProvider
from app.modules.calendar import CalendarProvider
from app.modules.fetch import JobPageFetcher
from app.modules.search import SearchProvider
from app.modules.secrets import SecretStore
loaded = sorted(name for name in {VENDOR_SDKS!r} if name in sys.modules and name not in before)
print(json.dumps(loaded))
"""


def test_module_interfaces_import_no_vendor_sdk() -> None:
    # Fresh interpreter so modules imported by other tests cannot hide or fake a leak.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", IMPORT_PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == []


def test_event_data_carries_a_stable_uid() -> None:
    # RFC 5545 needs a UID per event; re-exporting must update, not duplicate, the event.
    assert _event().uid == "event-0001"
    with pytest.raises(ValueError, match="uid"):
        _event(uid="")
