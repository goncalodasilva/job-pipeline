"""Usage on every provider call, against the real usage service (M2.16; AC-M2.6, AC-M2.8;
spec 3.7; ADR-056 follow-up, ADR-076 items 3 and 12, ADR-078, ADR-079).

Assumed interface (see also ``tests/unit/modules/ai/test_executor_usage.py``):

- ``app.services.usage_meter.ServiceUsageMeter(usage, service)`` implements
  ``app.modules.ai.UsageMeter`` over ``UsageService``: a ``request`` is reserved on
  ``service``, a ``search`` on ``web_search``; ``snapshot`` saves through ``save_snapshot``.
- ``ProviderSetupService(..., usage=UsageService)``: ``test_connection`` counts one request
  with ``skip_budget`` (task type ``connection_test``, tier ``fast``) on the service of the
  saved kind (``claude_subscription_token`` and ``claude_existing_login`` ->
  ``claude_subscription``; ``claude_api_key`` -> ``claude_api_key``; ``fake`` -> ``fake``),
  completes it whatever the outcome, and is never warned or blocked. A refused save counts
  nothing. The app passes the workspace's usage service.

Real migrated SQLite in a temp folder, ``FrozenClock``, ``SequentialIdFactory``, the scripted
fake provider and the fixture prompt copied under a task name the usage records accept. No
network, no sleeps. New names are imported inside the tests so each fails on its own until the
behaviour exists. All data is fictional.
"""

import asyncio
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.domain.budgets import Budget
from app.domain.errors import BudgetBlocked, UsageLimitReached
from app.domain.redaction import Redactor
from app.domain.settings import AIProviderSettings, Tier
from app.modules.ai import (
    AICapabilities,
    AIRequest,
    AIResponse,
    CancelToken,
    ConnectionResult,
    ProviderUsageSnapshot,
    TokenUsage,
)
from app.modules.ai.executor import TaskExecutor
from app.modules.ai.prompts import PromptLibrary
from app.modules.ai.providers.fake import (
    FAKE_FAST_MODEL,
    FAKE_THOROUGH_MODEL,
    FakeAIProvider,
    ScriptedResponse,
)
from app.modules.ai.tasks import TaskSpec
from app.modules.secrets.providers.memory import MemorySecretStore
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.services.provider_setup import ProviderSetupService
from app.services.settings import SettingsService
from app.services.usage import UsageService
from app.wiring import Wiring, build_wiring
from tests.fakes.ai import NotCancelled
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.fakes.keyring_backends import CountingKeyring

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PROMPTS = REPO_ROOT / "tests" / "fixtures" / "prompts"
FIXTURE_TASK = "_test_task"
TASK = "fictional_task"  # a task type the usage records accept
TZ = ZoneInfo("America/New_York")
# 10:00 local on 20 February 2026 (UTC-5): inside the cycle 15 February - 15 March.
START = datetime(2026, 2, 20, 15, 0, 0, tzinfo=UTC)
SERVICE = "fake"
WEB_SEARCH = "web_search"
WEB_SEARCH_TOOL = "web_search"
CONNECTION_TEST = "connection_test"
PROVIDER_DEFAULTS: dict[Tier, str] = {"fast": FAKE_FAST_MODEL, "thorough": FAKE_THOROUGH_MODEL}
INPUTS: dict[str, object] = {
    "job_text": "Example Ltd is hiring a fictional lighthouse keeper in a fictional town.",
    "preferences": {"target_roles": ["Lighthouse Keeper"], "locations": ["Fictionville"]},
}
OK_DATA = {"answer": "A fictional answer about a fictional role."}
TOKENS = TokenUsage(input_tokens=120, output_tokens=45)
TOKEN_KIND = "claude_subscription_token"  # noqa: S105 - a provider kind, not a secret
EXISTING_KIND = "claude_existing_login"
API_KEY_KIND = "claude_api_key"
FAKE_KIND = "fake"
TINY_TIMEOUT = 0.05  # seconds; the hanging provider never answers
FICTIONAL_TOKEN = "-".join(["sk", "ant", "oat01", "FICTIONALm216Tq7Wv3Kp9Xr2"])
FICTIONAL_API_KEY = "-".join(["FictionalPastedApiKey", "0216", "FICTIONAL", "Zr4Wq"])
CLAUDE_KINDS = [
    (TOKEN_KIND, FICTIONAL_TOKEN, "claude_subscription"),
    (EXISTING_KIND, "", "claude_subscription"),
    (API_KEY_KIND, FICTIONAL_API_KEY, "claude_api_key"),
]


def fake_key(keyword: str) -> str:
    return "-".join([keyword, "FICTIONAL", "m216", "Kd2Rv8Wz"])


def _snapshot(used: int) -> ProviderUsageSnapshot:
    return ProviderUsageSnapshot(
        service=SERVICE,
        window="month",
        unit="requests",
        used=used,
        limit=None,
        utilization=None,
        resets_at=None,
        observed_at=START,
    )


# --- fixtures and helpers ----------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "app.db"


@pytest.fixture
def db(tmp_path: Path, db_path: Path) -> Iterator[Database]:
    database = Database.open(db_path)
    apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def usage(db: Database) -> UsageService:
    return UsageService(db, FrozenClock(START), SequentialIdFactory("usage"), TZ)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "data"
    folder.joinpath("tmp", "ai").mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture
def prompts(tmp_path: Path) -> PromptLibrary:
    """The fixture prompt copied under ``TASK`` (same version, tier and schema)."""
    folder = tmp_path / "prompts" / TASK
    folder.mkdir(parents=True)
    text = (FIXTURE_PROMPTS / FIXTURE_TASK / "prompt.md").read_text(encoding="utf-8")
    (folder / "prompt.md").write_text(text.replace(FIXTURE_TASK, TASK), encoding="utf-8")
    schema = (FIXTURE_PROMPTS / FIXTURE_TASK / "schema.json").read_text(encoding="utf-8")
    (folder / "schema.json").write_text(schema, encoding="utf-8")
    return PromptLibrary(tmp_path / "prompts")


def _meter(usage: UsageService, service: str = SERVICE) -> Any:
    from app.services.usage_meter import ServiceUsageMeter

    return ServiceUsageMeter(usage, service)


def _executor(provider: Any, prompts: PromptLibrary, *, web: bool = False) -> TaskExecutor:
    return TaskExecutor(
        provider=provider,
        prompts=prompts,
        settings=AIProviderSettings,
        provider_defaults=PROVIDER_DEFAULTS,
        specs={TASK: TaskSpec(TASK, "summary", allow_web_search=web)},
    )


def _scripted(*responses: ScriptedResponse) -> FakeAIProvider:
    provider = FakeAIProvider()
    provider.script(TASK, *responses)
    return provider


async def _run(executor: TaskExecutor, meter: Any, **kwargs: Any) -> Any:
    return await executor.run_task(TASK, INPUTS, NotCancelled(), meter=meter, **kwargs)  # type: ignore[call-arg]


def _budget(**overrides: Any) -> Budget:
    values: dict[str, Any] = {
        "allowance": 10,
        "first_warning": 5,
        "second_warning": 8,
        "block_at": 10,
        "reset_day": 15,
    }
    values.update(overrides)
    return Budget(**values)


def _set_budget(usage: UsageService, service: str, budget: Budget) -> None:
    usage.save_budget(service, budget, expected_version=usage.load_budgets().version)


def _one_left(usage: UsageService, service: str) -> None:
    """A budget of three on ``service`` with two already used: one request is left."""
    _set_budget(usage, service, _budget(first_warning=1, second_warning=2, block_at=3))
    for _ in range(2):
        usage.complete(
            usage.reserve(service, tier="fast", task_type=TASK), success=True, tokens=None
        )


class RecordingProvider:
    """Answers the connection test with ``result``."""

    name = "recording"

    def __init__(self, result: ConnectionResult) -> None:
        self._result = result
        self.calls = 0

    async def test_connection(self, model: str) -> ConnectionResult:
        self.calls += 1
        return self._result


class HangingProvider:
    name = "hanging"

    async def test_connection(self, model: str) -> ConnectionResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


class ScriptedFactory:
    """A real wiring (fakes allowed, test keyring) whose built providers are replaced by
    ``provider`` when one is given."""

    def __init__(self, provider: Any | None = None) -> None:
        self._wiring: Wiring = build_wiring(
            environ={"JOBPIPELINE_ALLOW_FAKE_PROVIDERS": "1"},
            demo=False,
            redactor=Redactor(),
            keyring_backend=CountingKeyring(),
        )
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wiring, name)

    def ai_provider(self, settings: AIProviderSettings, *, data_dir: Path) -> Any:
        built = self._wiring.ai_provider(settings, data_dir=data_dir)
        return self._provider if self._provider is not None else built

    def fake_for_key(self, key: str | None) -> Any:
        built = self._wiring.fake_for_key(key)
        return self._provider if self._provider is not None else built


def _setup(
    db: Database, usage: UsageService, provider: Any | None = None, **kwargs: Any
) -> ProviderSetupService:
    return ProviderSetupService(
        SettingsService(db, FrozenClock(START)),
        MemorySecretStore(),
        ScriptedFactory(provider),  # type: ignore[arg-type]
        frozenset(),
        lambda: None,
        usage=usage,
        **kwargs,
    )


def _connected(model: str = "claude-haiku-fictional") -> RecordingProvider:
    return RecordingProvider(ConnectionResult(ok=True, message="Connected", model=model))


def _rows(usage: UsageService, service: str) -> list[tuple[str | None, str, int, int]]:
    return [
        (row.tier, row.task_type, row.requests, row.unfinished) for row in usage.breakdown(service)
    ]


# --- task calls --------------------------------------------------------------------------------


async def test_task_call_is_recorded_on_the_provider_service(
    usage: UsageService, prompts: PromptLibrary
) -> None:
    provider = _scripted(ScriptedResponse(data=OK_DATA, usage=TOKENS))

    await _run(_executor(provider, prompts), _meter(usage))

    (row,) = usage.breakdown(SERVICE)
    assert (row.tier, row.task_type, row.requests, row.unfinished) == ("fast", TASK, 1, 0)
    assert (row.input_tokens, row.output_tokens) == (120, 45)
    assert usage.status(WEB_SEARCH).local_count == 0


async def test_ac_m2_6_record_is_committed_before_the_provider_call(
    usage: UsageService, prompts: PromptLibrary, db_path: Path
) -> None:
    """While the call is in flight the reservation is already on disk and counted, so a crash
    at that point leaves it counted (a second connection sees it)."""
    seen: list[tuple[int, int]] = []

    class Inspecting(FakeAIProvider):
        async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
            other = Database.open(db_path)
            try:
                status = UsageService(
                    other, FrozenClock(START), SequentialIdFactory("other"), TZ
                ).status(SERVICE)
            finally:
                other.close()
            seen.append((status.local_count, status.unfinished_count))
            return await super().run(request, cancel)

    provider = Inspecting()
    provider.script(TASK, ScriptedResponse(data=OK_DATA))

    await _run(_executor(provider, prompts), _meter(usage))

    assert seen == [(1, 1)], "reserved and counted before the provider is called"
    after = usage.status(SERVICE)
    assert (after.local_count, after.unfinished_count) == (1, 0)


async def test_ac_m2_6_crash_inside_the_provider_leaves_the_record_counted(
    usage: UsageService, prompts: PromptLibrary
) -> None:
    class Crashing:
        name = "fake"
        capabilities = AICapabilities()

        async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
            raise RuntimeError("a fictional crash inside the provider")

    _one_left(usage, SERVICE)

    with pytest.raises(RuntimeError):
        await _run(_executor(Crashing(), prompts), _meter(usage))

    assert usage.status(SERVICE).local_count == 3
    with pytest.raises(BudgetBlocked):
        await _run(_executor(Crashing(), prompts), _meter(usage))
    assert usage.status(SERVICE).local_count == 3, "a blocked call writes nothing"


async def test_retry_is_a_second_counted_request(
    usage: UsageService, prompts: PromptLibrary
) -> None:
    provider = _scripted(
        ScriptedResponse(data={"answer": 42}, usage=TOKENS),
        ScriptedResponse(data=OK_DATA, usage=TOKENS),
    )

    result = await _run(_executor(provider, prompts), _meter(usage))

    assert result.attempts == 2
    (row,) = usage.breakdown(SERVICE)
    assert (row.requests, row.unfinished) == (2, 0)
    assert (row.input_tokens, row.output_tokens) == (240, 90)


async def test_retry_blocked_by_the_real_budget(
    usage: UsageService, prompts: PromptLibrary
) -> None:
    _one_left(usage, SERVICE)
    provider = _scripted(ScriptedResponse(data={"answer": 42}), ScriptedResponse(data=OK_DATA))

    with pytest.raises(BudgetBlocked):
        await _run(_executor(provider, prompts), _meter(usage))

    assert len(provider.requests) == 1
    status = usage.status(SERVICE)
    assert (status.local_count, status.unfinished_count) == (3, 0)


async def test_snapshot_reported_during_call_is_saved(
    usage: UsageService, prompts: PromptLibrary
) -> None:
    snapshot = _snapshot(7)
    provider = _scripted(ScriptedResponse(data=OK_DATA, usage_snapshots=(snapshot,)))

    await _run(_executor(provider, prompts), _meter(usage))

    status = usage.status(SERVICE)
    assert status.provider_snapshots == (snapshot,)
    assert status.provider_count == 7
    assert status.local_count == 1


async def test_ac_m2_8_provider_count_reported_during_a_call_is_used(
    usage: UsageService, prompts: PromptLibrary
) -> None:
    """The provider reports more requests than were counted here: its number is the one the
    budget uses, so the next call is blocked although only one was made locally."""
    _set_budget(usage, SERVICE, _budget())
    provider = _scripted(ScriptedResponse(data=OK_DATA, usage_snapshots=(_snapshot(10),)))
    executor = _executor(provider, prompts)

    await _run(executor, _meter(usage))

    status = usage.status(SERVICE)
    assert (status.local_count, status.provider_count, status.effective.value) == (1, 10, 10)
    with pytest.raises(BudgetBlocked):
        await _run(executor, _meter(usage))
    assert len(provider.requests) == 1, "the blocked call never reached the provider"


async def test_snapshot_of_a_failed_call_is_saved(
    usage: UsageService, prompts: PromptLibrary
) -> None:
    snapshot = _snapshot(9)
    provider = _scripted(ScriptedResponse(failure="usage_limit", usage_snapshots=(snapshot,)))

    with pytest.raises(UsageLimitReached):
        await _run(_executor(provider, prompts), _meter(usage))

    assert usage.status(SERVICE).provider_snapshots == (snapshot,)


async def test_web_searches_are_counted_on_the_web_search_service(
    usage: UsageService, prompts: PromptLibrary
) -> None:
    provider = _scripted(ScriptedResponse(data=OK_DATA, tool_calls=2))  # type: ignore[call-arg]

    await _run(_executor(provider, prompts, web=True), _meter(usage))

    assert provider.tool_calls == [(WEB_SEARCH_TOOL, True), (WEB_SEARCH_TOOL, True)]  # type: ignore[attr-defined]
    assert _rows(usage, WEB_SEARCH) == [("fast", TASK, 2, 0)]
    assert _rows(usage, SERVICE) == [("fast", TASK, 1, 0)], "searches aren't requests"


async def test_web_search_blocked_by_the_web_search_budget(
    usage: UsageService, prompts: PromptLibrary
) -> None:
    _one_left(usage, WEB_SEARCH)
    provider = _scripted(ScriptedResponse(data=OK_DATA, tool_calls=2))  # type: ignore[call-arg]

    result = await _run(_executor(provider, prompts, web=True), _meter(usage))

    assert provider.tool_calls == [(WEB_SEARCH_TOOL, True), (WEB_SEARCH_TOOL, False)]  # type: ignore[attr-defined]
    assert usage.status(WEB_SEARCH).local_count == 3
    assert dict(result.data) == OK_DATA


CONFORMANCE = """
from app.modules.ai import UsageMeter
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter


def build(usage: UsageService) -> UsageMeter:
    return ServiceUsageMeter(usage, "claude_subscription")
"""


def test_service_usage_meter_satisfies_the_protocol(tmp_path: Path) -> None:
    sample = tmp_path / "conformance.py"
    sample.write_text(CONFORMANCE, encoding="utf-8")
    # Fixed argv to the venv's own interpreter; no shell, no untrusted input.
    result = subprocess.run(  # noqa: S603
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
        env={**os.environ, "MYPYPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_meter_refuses_an_unknown_service(usage: UsageService) -> None:
    with pytest.raises(ValueError):
        _meter(usage, "fictional_service")


# --- the connection test (ADR-076 item 3) ------------------------------------------------------


async def test_connection_test_counts_one_request(
    db: Database, usage: UsageService, data_dir: Path
) -> None:
    result = await _setup(db, usage).test_connection(
        FAKE_KIND, fake_key("fake-connected"), data_dir=data_dir
    )

    assert result.ok is True, result
    assert _rows(usage, SERVICE) == [("fast", CONNECTION_TEST, 1, 0)]
    status = usage.status(SERVICE)
    assert (status.local_count, status.unfinished_count) == (1, 0)


@pytest.mark.parametrize(
    ("kind", "credential", "service"), CLAUDE_KINDS, ids=[kind for kind, _, _ in CLAUDE_KINDS]
)
async def test_connection_test_counts_on_the_service_of_the_kind(
    db: Database, usage: UsageService, data_dir: Path, kind: str, credential: str, service: str
) -> None:
    provider = _connected()

    result = await _setup(db, usage, provider).test_connection(kind, credential, data_dir=data_dir)

    assert result.ok is True, result
    assert provider.calls == 1
    assert _rows(usage, service) == [("fast", CONNECTION_TEST, 1, 0)]
    others = {"claude_subscription", "claude_api_key", "fake", "web_search"} - {service}
    assert {other: usage.status(other).local_count for other in others} == dict.fromkeys(others, 0)


async def test_connection_test_never_blocked_by_budget(
    db: Database, usage: UsageService, data_dir: Path
) -> None:
    _set_budget(usage, SERVICE, _budget())
    for _ in range(10):
        usage.complete(
            usage.reserve(SERVICE, tier="fast", task_type=TASK), success=True, tokens=None
        )
    with pytest.raises(BudgetBlocked):
        usage.reserve(SERVICE, tier="fast", task_type=TASK)

    result = await _setup(db, usage).test_connection(
        FAKE_KIND, fake_key("fake-connected"), data_dir=data_dir
    )

    assert result.ok is True, "the test runs although the budget is used up"
    assert result.message.startswith("Connected"), "no warning or block wording"
    status = usage.status(SERVICE)
    assert status.local_count == 11, "and it is still counted"
    assert status.shown_thresholds == frozenset(), "no warning was shown or recorded"


async def test_failed_connection_test_is_counted_and_completed(
    db: Database, usage: UsageService, data_dir: Path
) -> None:
    result = await _setup(db, usage).test_connection(
        FAKE_KIND, fake_key("fake-auth-rejected"), data_dir=data_dir
    )

    assert result.ok is False
    assert _rows(usage, SERVICE) == [("fast", CONNECTION_TEST, 1, 0)]


async def test_timed_out_connection_test_is_counted_and_completed(
    db: Database, usage: UsageService, data_dir: Path
) -> None:
    setup = _setup(db, usage, HangingProvider(), connection_timeout=TINY_TIMEOUT)

    result = await setup.test_connection(EXISTING_KIND, "", data_dir=data_dir)

    assert result.ok is False
    assert _rows(usage, "claude_subscription") == [("fast", CONNECTION_TEST, 1, 0)]


async def test_refused_save_counts_nothing(
    db: Database, usage: UsageService, data_dir: Path
) -> None:
    provider = _connected()

    result = await _setup(db, usage, provider).test_connection(TOKEN_KIND, "", data_dir=data_dir)

    assert result.ok is False and provider.calls == 0
    for service in ("claude_subscription", "claude_api_key", "fake", "web_search"):
        assert usage.status(service).local_count == 0


async def test_connection_test_counts_off_the_event_loop(db: Database, data_dir: Path) -> None:
    class ThreadRecordingUsage(UsageService):
        threads: list[int]

        def reserve(self, *args: Any, **kwargs: Any) -> str:
            self.threads.append(threading.get_ident())
            return super().reserve(*args, **kwargs)

        def complete(self, *args: Any, **kwargs: Any) -> None:
            self.threads.append(threading.get_ident())
            super().complete(*args, **kwargs)

    spy = ThreadRecordingUsage(db, FrozenClock(START), SequentialIdFactory("usage"), TZ)
    spy.threads = []

    await _setup(db, spy).test_connection(FAKE_KIND, fake_key("fake-connected"), data_dir=data_dir)

    assert len(spy.threads) == 2, "one reserve and one complete"
    assert threading.get_ident() not in spy.threads, "database work stays off the event loop"


def test_connection_test_through_the_app_counts_one_request(tmp_path: Path) -> None:
    """``POST /setup/provider/test`` in the running app (its own wiring of the usage service)."""
    from tests.integration.test_setup_provider import FAKE_KIND as APP_FAKE_KIND
    from tests.integration.test_setup_provider import running
    from tests.integration.test_setup_provider_test import FAKE_CONNECTED, post_test

    with running(tmp_path) as run:
        first = post_test(run, APP_FAKE_KIND, FAKE_CONNECTED)
        assert first.status_code == 200, first.text[:300]
        post_test(run, APP_FAKE_KIND, fake_key("fake-offline"))
        database: Database = run.app.state.workspace.db
        with database.transaction() as conn:
            rows = conn.execute(
                "SELECT service, task_type, tier, success FROM usage_records ORDER BY success DESC"
            ).fetchall()

    assert [tuple(row) for row in rows] == [
        ("fake", CONNECTION_TEST, "fast", 1),
        ("fake", CONNECTION_TEST, "fast", 0),
    ], "one counted, completed request per connection test"
