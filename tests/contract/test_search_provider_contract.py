"""M8 delegated search metadata; standalone calls cannot bypass the AI meter."""

import pytest

pytestmark = pytest.mark.contract


@pytest.fixture(params=["delegated_claude", "fake_cached", "fake_unclear"])
def search_provider(request):
    if request.param == "delegated_claude":
        from app.modules.search.providers.delegated import DelegatedAISearch

        return DelegatedAISearch()
    from tests.fakes.search import FakeSearchProvider

    return FakeSearchProvider(storage_allowed=True if request.param == "fake_cached" else None)


async def test_search_provider_contract(search_provider):
    assert search_provider.name in {"claude_builtin", "fake"}
    assert search_provider.mode == "delegated"
    assert search_provider.storage_allowed in (True, None)
    assert search_provider.reports_usage is False
    with pytest.raises(NotImplementedError):
        await search_provider.search("Fictional company", 5)


def test_wiring_only_selects_search_for_search_capable_ai():
    from app.domain.redaction import Redactor
    from app.modules.ai.providers.fake import FakeAIProvider
    from app.wiring import build_wiring
    from tests.fakes.keyring_backends import CountingKeyring

    wiring = build_wiring(
        environ={"JOBPIPELINE_ALLOW_FAKE_PROVIDERS": "1"},
        demo=False,
        redactor=Redactor(),
        keyring_backend=CountingKeyring(),
    )
    plain = FakeAIProvider()
    assert wiring.search_provider(plain) is None
    web = FakeAIProvider(web_search=True)
    assert wiring.search_provider(web).name == "fake"
