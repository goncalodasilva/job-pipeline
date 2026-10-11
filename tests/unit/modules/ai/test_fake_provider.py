"""FakeAIProvider behaviour (M1.10; AC-M1.3 fake side; spec 4.2; ADR-044 item 2).

The fake answers from scripts keyed by task and fixture, records every request, and
simulates the spec 2.11 failures, latency (through an injected sleep), invalid output and
token usage. Fictional keywords inside a pasted "key" choose its outcome in the setup
guide. Key-shaped strings are built at runtime and are fictional.
"""

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import (
    AppError,
    InvalidModelOutput,
    NetworkUnavailable,
    ProviderAuthError,
    ProviderNotInstalled,
    UsageLimitReached,
)
from app.modules.ai import AICapabilities, ModelChoice, TokenUsage
from app.modules.ai.providers.fake import (
    FAKE_FAST_MODEL,
    FAKE_THOROUGH_MODEL,
    FakeAIProvider,
    FakeOutcome,
    ScriptedResponse,
    outcome_for_key,
)
from tests.fakes.ai import NotCancelled, load_ai_fixture, make_request

REPO_ROOT = Path(__file__).resolve().parents[4]
TEST_TASK = "_test_task"

FAILURES: dict[FakeOutcome, type[AppError]] = {
    "auth": ProviderAuthError,
    "not_installed": ProviderNotInstalled,
    "usage_limit": UsageLimitReached,
    "network": NetworkUnavailable,
}


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _fictional_key(keyword: str) -> str:
    # Built at runtime so nothing key-shaped sits in the source.
    return "sk-" + "fictional-" + "a1b2" * 3 + "-" + keyword + "-" + "z9" * 4


async def test_fake_records_requests() -> None:
    fake = FakeAIProvider()
    fake.script("task_one", ScriptedResponse(data={"answer": "one"}))
    fake.script("task_two", ScriptedResponse(data={"answer": "two"}))
    first = make_request("task_one")
    second = make_request("task_two", model=FAKE_THOROUGH_MODEL, allow_web_search=True)

    await fake.run(first, NotCancelled())
    await fake.run(second, NotCancelled())

    assert fake.requests == [first, second]


async def test_fake_scripted_by_task_and_fixture() -> None:
    fake = FakeAIProvider()
    ok = load_ai_fixture(TEST_TASK, "ok")
    with_usage = load_ai_fixture(TEST_TASK, "usage")
    fake.script(TEST_TASK, ok, with_usage)
    fake.script("other_task", ScriptedResponse(data={"answer": "other"}))

    first = await fake.run(make_request(TEST_TASK), NotCancelled())
    other = await fake.run(make_request("other_task"), NotCancelled())
    second = await fake.run(make_request(TEST_TASK), NotCancelled())
    repeated = await fake.run(make_request(TEST_TASK), NotCancelled())

    assert ok.data is not None and with_usage.data is not None
    assert dict(first.data) == dict(ok.data)
    assert json.loads(first.raw_text) == dict(ok.data)
    assert dict(other.data) == {"answer": "other"}
    assert dict(second.data) == dict(with_usage.data)
    assert second.usage == TokenUsage(input_tokens=120, output_tokens=45)
    # The last scripted response repeats once the queue is used up.
    assert dict(repeated.data) == dict(with_usage.data)
    assert repeated.usage == second.usage


async def test_fake_simulates_latency_with_injected_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never_really_wait(seconds: float) -> None:
        raise AssertionError(f"asyncio.sleep({seconds}) was called; the fake must not wait")

    monkeypatch.setattr(asyncio, "sleep", never_really_wait)
    sleep = RecordingSleep()
    fake = FakeAIProvider(sleep=sleep)
    fake.script(TEST_TASK, ScriptedResponse(data={"answer": "slow"}, delay_seconds=2.5))

    response = await fake.run(make_request(TEST_TASK), NotCancelled())

    assert sleep.delays == [2.5]
    assert dict(response.data) == {"answer": "slow"}


async def test_fake_reports_token_usage() -> None:
    fake = FakeAIProvider()
    assert fake.capabilities == AICapabilities(reports_token_usage=True)
    assert fake.capabilities.web_search is False
    fake.script(
        TEST_TASK,
        ScriptedResponse(data={"answer": "x"}, usage=TokenUsage(7, 3)),
        ScriptedResponse(data={"answer": "y"}),
    )

    reported = await fake.run(make_request(TEST_TASK), NotCancelled())
    unreported = await fake.run(make_request(TEST_TASK), NotCancelled())

    assert reported.usage == TokenUsage(input_tokens=7, output_tokens=3)
    assert unreported.usage is None


@pytest.mark.parametrize(
    "scripted",
    [
        load_ai_fixture(TEST_TASK, "malformed"),
        ScriptedResponse(raw_text='[{"answer": "a list, not an object"}]'),
        ScriptedResponse(raw_text='"just a string"'),
        ScriptedResponse(raw_text='{"answer": "truncated'),
    ],
    ids=["malformed_fixture", "json_array", "json_string", "truncated_object"],
)
async def test_fake_invalid_output_scripted(scripted: ScriptedResponse) -> None:
    fake = FakeAIProvider()
    fake.script(TEST_TASK, scripted)

    with pytest.raises(InvalidModelOutput):
        await fake.run(make_request(TEST_TASK), NotCancelled())


async def test_fake_raw_text_used_verbatim_when_it_is_a_json_object() -> None:
    fake = FakeAIProvider()
    raw = '{"answer":  "spaced"}'
    fake.script(TEST_TASK, ScriptedResponse(raw_text=raw))

    response = await fake.run(make_request(TEST_TASK), NotCancelled())

    assert response.raw_text == raw
    assert dict(response.data) == {"answer": "spaced"}


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("fake-auth-rejected", "auth"),
        ("fake-not-installed", "not_installed"),
        ("fake-usage-limit", "usage_limit"),
        ("fake-offline", "network"),
        ("fake-connected", "connected"),
    ],
)
def test_fake_key_keywords_select_outcome(keyword: str, expected: FakeOutcome) -> None:
    assert outcome_for_key(keyword) == expected
    assert outcome_for_key(_fictional_key(keyword)) == expected
    assert outcome_for_key(keyword + "-" + "q" * 10) == expected
    assert outcome_for_key("x" * 6 + keyword) == expected


@pytest.mark.parametrize(
    "key",
    [None, "", "   ", "fictional-unknown-key", "fake", "fake-connect"],
    ids=["none", "empty", "blank", "unknown", "bare_fake", "partial_keyword"],
)
def test_fake_key_without_keyword_is_rejected_as_auth(key: str | None) -> None:
    assert outcome_for_key(key) == "auth"


@pytest.mark.parametrize(
    "key",
    [None, "", "   ", "fictional-unknown-key", "fake", "fake-connect"],
    ids=["none", "empty", "blank", "unknown", "bare_fake", "partial_keyword"],
)
def test_fake_key_without_keyword_is_what_the_caller_says(key: str | None) -> None:
    """M2.26: the demo's wiring asks for "connected" when nothing useful was pasted."""
    assert outcome_for_key(key, otherwise="connected") == "connected"
    assert outcome_for_key(key, otherwise="network") == "network"


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [("fake-auth-rejected", "auth"), ("fake-offline", "network"), ("fake-connected", "connected")],
)
def test_fake_key_keyword_wins_over_what_the_caller_says(
    keyword: str, expected: FakeOutcome
) -> None:
    assert outcome_for_key(_fictional_key(keyword), otherwise="connected") == expected
    assert outcome_for_key(_fictional_key(keyword), otherwise="usage_limit") == expected


@pytest.mark.parametrize("outcome", list(FAILURES), ids=list(FAILURES))
async def test_ac_m1_3_fake_key_outcome_shows_mapped_message(outcome: FakeOutcome) -> None:
    keyword = {
        "auth": "fake-auth-rejected",
        "not_installed": "fake-not-installed",
        "usage_limit": "fake-usage-limit",
        "network": "fake-offline",
    }[outcome]
    fake = FakeAIProvider(connection=outcome_for_key(_fictional_key(keyword)))

    result = await fake.test_connection(FAKE_FAST_MODEL)

    assert result.ok is False
    assert result.message == FAILURES[outcome]().user_message
    assert result.model is None


async def test_ac_m1_3_fake_connected_key_shows_connected() -> None:
    fake = FakeAIProvider(connection=outcome_for_key(_fictional_key("fake-connected")))

    result = await fake.test_connection(FAKE_THOROUGH_MODEL)

    assert result.ok is True
    assert result.message == "Connected"
    assert result.model == FAKE_THOROUGH_MODEL


@pytest.mark.parametrize(("outcome", "error"), list(FAILURES.items()), ids=list(FAILURES))
async def test_fake_failures_raise_spec_errors(outcome: FakeOutcome, error: type[AppError]) -> None:
    fake = FakeAIProvider()
    fake.script(TEST_TASK, ScriptedResponse(failure=outcome))
    request = make_request(TEST_TASK)

    with pytest.raises(error) as raised:
        await fake.run(request, NotCancelled())

    assert raised.value.user_message == error().user_message
    assert fake.requests == [request]  # the call was attempted, so it is recorded


async def test_fake_scripted_failure_then_answer_consumes_queue_in_order() -> None:
    fake = FakeAIProvider()
    fake.script(
        TEST_TASK, ScriptedResponse(failure="usage_limit"), load_ai_fixture(TEST_TASK, "ok")
    )

    with pytest.raises(UsageLimitReached):
        await fake.run(make_request(TEST_TASK), NotCancelled())
    recovered = await fake.run(make_request(TEST_TASK), NotCancelled())

    assert recovered.data


async def test_fake_unscripted_task_raises_lookup_error() -> None:
    fake = FakeAIProvider()
    fake.script("scripted_task", ScriptedResponse(data={"answer": "x"}))

    with pytest.raises(LookupError, match="unscripted_task"):
        await fake.run(make_request("unscripted_task"), NotCancelled())


async def test_fake_adversarial_fixture_is_returned_as_data() -> None:
    adversarial = load_ai_fixture(TEST_TASK, "adversarial")
    assert adversarial.data is not None
    fake = FakeAIProvider()
    fake.script(TEST_TASK, adversarial)
    request = make_request(TEST_TASK)

    response = await fake.run(request, NotCancelled())

    # The provider passes content through untouched; flagging it is the task layer's job.
    assert dict(response.data) == dict(adversarial.data)
    assert "Ignore previous instructions" in response.data["answer"]
    assert response.model == request.model
    assert fake.requests == [request]
    assert fake.capabilities.web_search is False


def test_fake_known_models_are_fast_and_thorough() -> None:
    assert FakeAIProvider().known_models() == [
        ModelChoice(id=FAKE_FAST_MODEL, label="Fake fast"),
        ModelChoice(id=FAKE_THOROUGH_MODEL, label="Fake thorough"),
    ]
    assert FakeAIProvider().name == "fake"


CONFORMANCE = """\
from app.modules.ai import AIProvider
from app.modules.ai.providers.fake import FakeAIProvider

default: AIProvider = FakeAIProvider()
failing: AIProvider = FakeAIProvider(connection="network")
"""


def test_fake_satisfies_ai_provider_protocol(tmp_path: Path) -> None:
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


async def test_fake_cancelled_during_the_call_raises_call_cancelled() -> None:
    from app.modules.ai import CallCancelled

    class Token:
        cancelled = False

    token = Token()

    async def cancel_while_waiting(seconds: float) -> None:
        token.cancelled = True

    fake = FakeAIProvider(sleep=cancel_while_waiting)
    fake.script("_test_task", ScriptedResponse(data={"answer": "ok"}, delay_seconds=1.0))

    with pytest.raises(CallCancelled):
        await fake.run(make_request(task="_test_task"), token)


def test_fake_scripted_response_needs_content_or_failure() -> None:
    with pytest.raises(ValueError):
        ScriptedResponse()
    with pytest.raises(ValueError):
        ScriptedResponse(failure="connected")
    assert ScriptedResponse(failure="network").failure == "network"


async def test_fake_app_mode_keeps_no_requests_and_answers_unscripted_with_plain_error() -> None:
    fake = FakeAIProvider(app_mode=True)

    with pytest.raises(InvalidModelOutput):
        await fake.run(make_request(task="unscripted_task"), NotCancelled())
    fake.script("_test_task", ScriptedResponse(data={"answer": "ok"}))
    await fake.run(make_request(task="_test_task"), NotCancelled())

    assert fake.requests == []


# --- provider-reported usage (M2.2; groundwork for AC-M2.8) -------------------------------

USAGE_OBSERVED = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _fake_snapshot(used: int = 7) -> Any:
    from app.modules.ai import ProviderUsageSnapshot

    return ProviderUsageSnapshot(
        service="fake",
        window="month",
        unit="requests",
        used=used,
        limit=100,
        utilization=None,
        resets_at=USAGE_OBSERVED + timedelta(days=20),
        observed_at=USAGE_OBSERVED,
    )


async def test_fake_reports_scripted_snapshot() -> None:
    snapshot = _fake_snapshot()
    fake = FakeAIProvider()
    fake.script(
        TEST_TASK,
        ScriptedResponse(data={"answer": "counted"}, usage_snapshots=(snapshot,)),
    )
    reported: list[Any] = []
    request = dataclasses.replace(make_request(TEST_TASK), on_usage=reported.append)

    response = await fake.run(request, NotCancelled())

    assert reported == [snapshot]
    assert dict(response.data) == {"answer": "counted"}


async def test_fake_reports_each_scripted_snapshot_in_order() -> None:
    first, second = _fake_snapshot(7), _fake_snapshot(8)
    fake = FakeAIProvider()
    fake.script(
        TEST_TASK,
        ScriptedResponse(data={"answer": "x"}, usage_snapshots=(first, second)),
    )
    reported: list[Any] = []
    request = dataclasses.replace(make_request(TEST_TASK), on_usage=reported.append)

    await fake.run(request, NotCancelled())

    assert reported == [first, second]


async def test_fake_reports_no_snapshot_unless_scripted() -> None:
    fake = FakeAIProvider()
    fake.script(TEST_TASK, ScriptedResponse(data={"answer": "x"}))
    reported: list[Any] = []
    request = dataclasses.replace(make_request(TEST_TASK), on_usage=reported.append)

    await fake.run(request, NotCancelled())

    assert reported == []


async def test_fake_scripted_snapshot_without_on_usage_is_harmless() -> None:
    fake = FakeAIProvider()
    fake.script(
        TEST_TASK,
        ScriptedResponse(data={"answer": "x"}, usage_snapshots=(_fake_snapshot(),)),
    )

    response = await fake.run(make_request(TEST_TASK), NotCancelled())

    assert dict(response.data) == {"answer": "x"}


async def test_fake_raising_on_usage_keeps_the_result() -> None:
    fake = FakeAIProvider()
    fake.script(
        TEST_TASK,
        ScriptedResponse(data={"answer": "x"}, usage_snapshots=(_fake_snapshot(),)),
    )
    calls: list[Any] = []

    def raising(snapshot: Any) -> None:
        calls.append(snapshot)
        raise RuntimeError("Fictional callback failure")

    request = dataclasses.replace(make_request(TEST_TASK), on_usage=raising)

    response = await fake.run(request, NotCancelled())

    assert calls
    assert dict(response.data) == {"answer": "x"}
