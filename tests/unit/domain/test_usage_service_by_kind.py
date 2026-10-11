"""Every provider kind counts its requests on a budget service (M2.16; ADR-076 item 4)."""

from typing import get_args

from app.domain.settings import ProviderKind
from app.domain.usage import SERVICE_BY_PROVIDER_KIND, SERVICE_NAMES, WEB_SEARCH_SERVICE


def test_every_provider_kind_has_a_usage_service() -> None:
    assert set(SERVICE_BY_PROVIDER_KIND) == set(get_args(ProviderKind))
    assert set(SERVICE_BY_PROVIDER_KIND.values()) <= set(SERVICE_NAMES)
    assert WEB_SEARCH_SERVICE not in SERVICE_BY_PROVIDER_KIND.values()
    shared = {SERVICE_BY_PROVIDER_KIND[kind] for kind in get_args(ProviderKind)[:2]}
    assert get_args(ProviderKind)[:2] == ("claude_subscription_token", "claude_existing_login")
    assert shared == {"claude_subscription"}, "both subscription sign-ins share one budget"
