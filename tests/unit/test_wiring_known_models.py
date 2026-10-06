"""``Wiring.known_models`` (M1.21): the models each kind's provider offers, without building
one, and the same list the built provider reports (spec 3.7). No keychain, CLI or network."""

from pathlib import Path

import pytest

from app.domain.redaction import Redactor
from app.domain.settings import AIProviderSettings, ProviderKind
from app.modules.ai import ModelChoice
from app.modules.ai.providers.claude_sdk import (
    CLAUDE_FAST_DEFAULT,
    CLAUDE_MOST_CAPABLE,
    CLAUDE_THOROUGH_DEFAULT,
)
from app.modules.ai.providers.fake import FAKE_FAST_MODEL, FAKE_THOROUGH_MODEL
from app.wiring import Wiring, build_wiring
from tests.fakes.keyring_backends import CountingKeyring

KINDS: tuple[ProviderKind, ...] = (
    "claude_subscription_token",
    "claude_existing_login",
    "claude_api_key",
    "fake",
)
CLAUDE_IDS = [CLAUDE_FAST_DEFAULT, CLAUDE_THOROUGH_DEFAULT, CLAUDE_MOST_CAPABLE]


def wiring(backend: CountingKeyring) -> Wiring:
    return build_wiring(
        environ={"JOBPIPELINE_ALLOW_FAKE_PROVIDERS": "1"},
        demo=False,
        redactor=Redactor(),
        keyring_backend=backend,
    )


@pytest.mark.parametrize("kind", KINDS)
def test_known_models_match_the_built_provider(kind: ProviderKind, tmp_path: Path) -> None:
    backend = CountingKeyring()
    factory = wiring(backend)
    provider = factory.ai_provider(AIProviderSettings(kind=kind), data_dir=tmp_path / "data")
    assert factory.known_models(kind) == tuple(provider.known_models())
    assert backend.calls == 0, "no keychain read"


def test_known_models_per_provider() -> None:
    backend = CountingKeyring()
    factory = wiring(backend)
    for kind in ("claude_subscription_token", "claude_existing_login", "claude_api_key"):
        models = factory.known_models(kind)  # type: ignore[arg-type]
        assert [model.id for model in models] == CLAUDE_IDS, kind
        assert all(isinstance(model, ModelChoice) and model.label for model in models)
    assert [model.id for model in factory.known_models("fake")] == [
        FAKE_FAST_MODEL,
        FAKE_THOROUGH_MODEL,
    ]
    # Nothing chosen yet counts as Claude, like ``connection_model``.
    assert factory.known_models(None) == factory.known_models("claude_api_key")
    # Offered or not, a kind's models are the same (nothing is built).
    gated = build_wiring(environ={}, demo=True, redactor=Redactor(), keyring_backend=backend)
    assert gated.known_models("fake") == factory.known_models("fake")
    assert backend.calls == 0
