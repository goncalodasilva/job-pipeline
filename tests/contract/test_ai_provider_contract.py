"""Shared AIProvider contract (M1.10; AC-M1.3; spec 2.4, 2.11, 4.1; ADR-044, ADR-051).

Every AI provider must answer with the same shape, report connection results with the
spec 2.11 messages and the failure kind (``ConnectionResult.failure``, ADR-063 follow-up),
list unique models, send no tools unless web search is allowed and make no call once
cancelled, and stop and release a call cancelled while it runs. Providers
join through a harness (``ai_harness.py``). The Claude provider runs the call and model
tests with a fake SDK client (M1.13) and the connection tests (``connection_harness``)
since M1.14. All values are fictional.
"""

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from app.domain.errors import (
    AppError,
    NetworkUnavailable,
    ProviderAuthError,
    ProviderNotInstalled,
    UsageLimitReached,
)
from app.modules.ai import AICapabilities, AIResponse, CallCancelled
from tests.contract.ai_harness import (
    CONTRACT_ANSWER,
    ClaudeTransportHarness,
    ConnectionHarness,
    Failure,
    FakeHarness,
    ProviderHarness,
)
from tests.fakes.ai import Cancelled, NotCancelled, make_request

pytestmark = pytest.mark.contract

# Each harness gets a fresh temporary folder (the Claude provider's temp root).
HARNESSES: dict[str, Callable[[Path], ProviderHarness]] = {
    "fake": lambda _root: FakeHarness(),
    "claude_sdk_fake_transport": lambda root: ClaudeTransportHarness(temp_root=root),
}
CONNECTION_HARNESSES: dict[str, Callable[[Path], ConnectionHarness]] = {
    "fake": lambda _root: FakeHarness(),
    "claude_sdk_fake_transport": lambda root: ClaudeTransportHarness(temp_root=root),
}

EXPECTED_ERROR: dict[Failure, type[AppError]] = {
    "auth": ProviderAuthError,
    "not_installed": ProviderNotInstalled,
    "usage_limit": UsageLimitReached,
    "network": NetworkUnavailable,
}


@pytest.fixture(params=list(HARNESSES), ids=list(HARNESSES))
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> ProviderHarness:
    return HARNESSES[request.param](tmp_path)


@pytest.fixture(params=list(CONNECTION_HARNESSES), ids=list(CONNECTION_HARNESSES))
def connection_harness(request: pytest.FixtureRequest, tmp_path: Path) -> ConnectionHarness:
    return CONNECTION_HARNESSES[request.param](tmp_path)


async def test_run_returns_parsed_object_shape(harness: ProviderHarness) -> None:
    provider = harness.provider()
    request = make_request()

    response = await provider.run(request, NotCancelled())

    assert isinstance(response, AIResponse)
    assert dict(response.data) == CONTRACT_ANSWER
    assert json.loads(response.raw_text) == CONTRACT_ANSWER
    assert response.model == harness.reported_model(request.model)


async def test_test_connection_ok_returns_connected(
    connection_harness: ConnectionHarness,
) -> None:
    harness = connection_harness
    provider = harness.provider()
    model = provider.known_models()[0].id

    result = await provider.test_connection(model)

    assert result.ok is True
    assert result.message == "Connected"
    assert result.model == harness.reported_model(model)
    assert result.failure is None


@pytest.mark.parametrize("outcome", list(EXPECTED_ERROR), ids=list(EXPECTED_ERROR))
async def test_test_connection_maps_failures(
    connection_harness: ConnectionHarness, outcome: Failure
) -> None:
    provider = connection_harness.failing(outcome)
    model = provider.known_models()[0].id

    result = await provider.test_connection(model)

    assert result.ok is False
    assert result.message == EXPECTED_ERROR[outcome]().user_message
    assert result.model is None
    assert result.failure == outcome, "every provider says what kind of failure it was"


def test_known_models_nonempty_unique(harness: ProviderHarness) -> None:
    models = harness.provider().known_models()

    assert models
    ids = [model.id for model in models]
    assert len(ids) == len(set(ids))
    assert all(model.id.strip() and model.label.strip() for model in models)


async def test_run_without_web_search_sends_no_tools(harness: ProviderHarness) -> None:
    provider = harness.provider()
    request = make_request()
    assert request.allow_web_search is False

    await provider.run(request, NotCancelled())

    assert harness.tools_sent() == [False]


async def test_cancelled_token_before_call_makes_no_call(harness: ProviderHarness) -> None:
    provider = harness.provider()

    with pytest.raises(CallCancelled):
        await provider.run(make_request(), Cancelled())

    assert harness.calls_made() == 0


async def test_cancel_during_call_raises_call_cancelled_and_releases_the_call(
    harness: ProviderHarness,
) -> None:
    provider, token = harness.cancelled_mid_call()

    with pytest.raises(CallCancelled):
        await provider.run(make_request(), token)

    assert harness.calls_made() == 1
    assert harness.released_before_return()


async def test_capabilities_declared_honestly(harness: ProviderHarness) -> None:
    provider = harness.provider()
    capabilities = provider.capabilities

    assert isinstance(capabilities, AICapabilities)
    # M1 providers offer no speech.
    assert capabilities.speech_to_text is False
    assert capabilities.text_to_speech is False

    response = await provider.run(make_request(), NotCancelled())
    # A provider that doesn't declare token usage must not invent it.
    if not capabilities.reports_token_usage:
        assert response.usage is None


def test_provider_name_is_stable(harness: ProviderHarness) -> None:
    first = harness.provider().name
    second = harness.provider().name

    assert first == second
    assert first
    assert first == first.strip().lower()


async def test_provider_usage_snapshot_shape(harness: ProviderHarness) -> None:
    from app.modules.ai import ProviderUsageSnapshot

    provider = harness.reporting_usage()
    reported: list[object] = []
    request = dataclasses.replace(make_request(), on_usage=reported.append)

    response = await provider.run(request, NotCancelled())

    assert dict(response.data) == CONTRACT_ANSWER
    assert len(reported) == 1, "each harness scripts exactly one snapshot"
    for snapshot in reported:
        assert isinstance(snapshot, ProviderUsageSnapshot)
        assert snapshot.service.strip() and snapshot.window.strip()
        assert snapshot.unit in ("requests", "fraction")
        for count in (snapshot.used, snapshot.limit):
            assert count is None or (isinstance(count, int) and count >= 0)
        if snapshot.unit == "requests":
            assert snapshot.used is not None
        else:
            assert snapshot.utilization is not None
        if snapshot.utilization is not None:
            assert 0.0 <= snapshot.utilization <= 1.0
        assert snapshot.observed_at.utcoffset() is not None
        assert snapshot.resets_at is None or snapshot.resets_at.utcoffset() is not None
