"""Changing the provider re-checks the tier map in the same save (M1.21; spec 3.7; ADR-051
follow-up, user decision: re-check rather than reset).

Tiers map to the active provider's models, so when the provider changes (Claude and the fake)
each tier keeps its model only if the new provider offers it (``known_models``) and goes back to
the provider's default (``None``) otherwise. Saving the same kind leaves the map alone, and so
do switching between Claude's sign-in routes and removing a saved key. Every value is
fictional.
"""

from pathlib import Path

import pytest

from app.domain.settings import AI_PROVIDER, AIProviderSettings, TierMap
from app.modules.ai.providers.claude_sdk import CLAUDE_MOST_CAPABLE, CLAUDE_THOROUGH_DEFAULT
from app.modules.ai.providers.fake import FAKE_FAST_MODEL, FAKE_THOROUGH_MODEL
from app.modules.storage import Database
from app.services.settings import SettingsService
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import MemoryKeyring
from tests.integration.test_setup_provider import (
    API_KEY_KIND,
    EXISTING_KIND,
    FAKE_KIND,
    FICTIONAL_TOKEN,
    TOKEN_KIND,
    stored,
)
from tests.integration.test_setup_provider_test import PASTED_API_KEY
from tests.integration.test_setup_redirect import START, Running, assert_see_other
from tests.integration.test_setup_reopen_banner import finished_guide, started

pytestmark = pytest.mark.integration


def save_kind(run: Running, kind: str, credential: str = "") -> None:
    """Save ``kind`` from Settings (the key field empty unless ``credential`` is given)."""
    response = run.client.post(
        "/settings/providers", data={"kind": kind, "credential": credential}, headers=run.action()
    )
    assert_see_other(response, "/settings")


def remove_key(run: Running, kind: str) -> None:
    response = run.client.post("/settings/providers", data={"remove": kind}, headers=run.action())
    assert_see_other(response, "/settings")


def set_tiers(run: Running, tiers: TierMap) -> None:
    """Store a tier map directly (Settings maps tiers from M1.22)."""
    db: Database = run.app.state.workspace.db
    service = SettingsService(db, FrozenClock(START))
    loaded = service.load(AI_PROVIDER)
    service.save(
        AI_PROVIDER,
        loaded.value.model_copy(update={"tiers": tiers}),
        expected_version=loaded.version,
    )


def test_changing_provider_keeps_offered_tier_models_and_resets_the_rest(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    # A mixed map (possible before M1.22 checks it): a fake model and a Claude one.
    mixed = TierMap(fast=FAKE_FAST_MODEL, thorough=CLAUDE_MOST_CAPABLE)
    finished_guide(tmp_path, provider=AIProviderSettings(kind=FAKE_KIND, tiers=mixed))
    with started(tmp_path) as run:
        save_kind(run, FAKE_KIND)
        assert stored(run).tiers == mixed, "the same kind leaves the map alone"

        # Fake to Claude: the Claude model stays, the fake one goes back to the default.
        save_kind(run, EXISTING_KIND)
        assert stored(run).tiers == TierMap(fast=None, thorough=CLAUDE_MOST_CAPABLE)

        # Claude to fake: no Claude model is offered by the fake.
        set_tiers(run, TierMap(fast=CLAUDE_THOROUGH_DEFAULT, thorough=CLAUDE_MOST_CAPABLE))
        save_kind(run, FAKE_KIND)
        assert stored(run).tiers == TierMap()

        # Fake to Claude with only fake models: both reset.
        set_tiers(run, TierMap(fast=FAKE_FAST_MODEL, thorough=FAKE_THOROUGH_MODEL))
        save_kind(run, EXISTING_KIND)
        assert stored(run).tiers == TierMap()
    assert memory_keyring.items == {}, "nothing was written to the keychain"


def test_switching_claude_routes_and_removing_a_key_keep_the_tier_map(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    """Claude's sign-in routes share Claude's models: a full Claude map survives token to
    existing sign-in to API key, and removing a saved key leaves it alone."""
    claude = TierMap(fast=CLAUDE_THOROUGH_DEFAULT, thorough=CLAUDE_MOST_CAPABLE)
    finished_guide(tmp_path, provider=AIProviderSettings(kind=FAKE_KIND))
    with started(tmp_path) as run:
        save_kind(run, TOKEN_KIND, FICTIONAL_TOKEN)
        set_tiers(run, claude)

        save_kind(run, EXISTING_KIND)
        assert (stored(run).kind, stored(run).tiers) == (EXISTING_KIND, claude)

        save_kind(run, API_KEY_KIND, PASTED_API_KEY)
        settings = stored(run)
        assert (settings.kind, settings.tiers) == (API_KEY_KIND, claude)
        assert settings.saved_sign_ins == (API_KEY_KIND, TOKEN_KIND)

        remove_key(run, TOKEN_KIND)
        settings = stored(run)
        assert (settings.kind, settings.saved_sign_ins) == (API_KEY_KIND, (API_KEY_KIND,))
        assert settings.tiers == claude, "removing a key changes no tier"
