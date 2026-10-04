"""Shared AIProvider contract (M1.10; AC-M1.3; spec 2.4, 2.11, 4.1; ADR-044, ADR-051).

Every AI provider must answer with the same shape, report connection results with the
spec 2.11 messages, list unique models, send no tools unless web search is allowed and
make no call once cancelled. Providers join through a harness (``ai_harness.py``); the
Claude provider joins in M1.13 with a fake transport. All values are fictional.
"""

import json
from collections.abc import Callable

import pytest

from app.domain.errors import (
    AppError,
    NetworkUnavailable,
    ProviderAuthError,
    ProviderNotInstalled,
    UsageLimitReached,
)
from app.modules.ai import AICapabilities, AIResponse, CallCancelled
from tests.contract.ai_harness import CONTRACT_ANSWER, Failure, FakeHarness, ProviderHarness
from tests.fakes.ai import Cancelled, NotCancelled, make_request

pytestmark = pytest.mark.contract

HARNESSES: dict[str, Callable[[], ProviderHarness]] = {"fake": FakeHarness}

EXPECTED_ERROR: dict[Failure, type[AppError]] = {
    "auth": ProviderAuthError,
    "not_installed": ProviderNotInstalled,
    "usage_limit": UsageLimitReached,
    "network": NetworkUnavailable,
}


@pytest.fixture(params=list(HARNESSES), ids=list(HARNESSES))
def harness(request: pytest.FixtureRequest) -> ProviderHarness:
    return HARNESSES[request.param]()


async def test_run_returns_parsed_object_shape(harness: ProviderHarness) -> None:
    provider = harness.provider()
    request = make_request()

    response = await provider.run(request, NotCancelled())

    assert isinstance(response, AIResponse)
    assert dict(response.data) == CONTRACT_ANSWER
    assert json.loads(response.raw_text) == CONTRACT_ANSWER
    assert response.model == harness.reported_model(request.model)


async def test_test_connection_ok_returns_connected(harness: ProviderHarness) -> None:
    provider = harness.provider()
    model = provider.known_models()[0].id

    result = await provider.test_connection(model)

    assert result.ok is True
    assert result.message == "Connected"
    assert result.model == harness.reported_model(model)


@pytest.mark.parametrize("outcome", list(EXPECTED_ERROR), ids=list(EXPECTED_ERROR))
async def test_test_connection_maps_failures(harness: ProviderHarness, outcome: Failure) -> None:
    provider = harness.failing(outcome)
    model = provider.known_models()[0].id

    result = await provider.test_connection(model)

    assert result.ok is False
    assert result.message == EXPECTED_ERROR[outcome]().user_message
    assert result.model is None


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
