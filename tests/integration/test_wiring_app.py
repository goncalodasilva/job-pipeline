"""Wiring inside the app: injected wiring, provider health at start-up and after a workspace
opens, and no recomputation on GET (M1.15).

AC-M1.1 case 4: a finished setup whose provider is broken opens the app with a "provider"
banner, never the wizard. The health that feeds that decision is configuration only (no
network, no CLI run, no keychain read; ADR-046) and is computed when a workspace opens, at
start-up or through ``app.state.open_workspace``, never on GET (spec 2.9.12; ADR-044, ADR-053).
Opening a workspace also clears leftover AI call folders in ``<data>/tmp/ai`` (ADR-057).

Every wiring is built from an explicit environment and a counting in-memory keyring backend;
the Claude SDK's bundled CLI is a placeholder under ``tmp_path`` that is never run. Folders
live under ``tmp_path`` and every value is fictional.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import __version__
from app.api.security import TOKEN_HEADER
from app.config import RuntimeConfig, write_bootstrap
from app.domain.errors import DataFolderUnavailable
from app.domain.redaction import Redactor
from app.domain.settings import AI_PROVIDER, AIProviderSettings, SavedSignInKind
from app.main import create_app
from app.modules.ai import ProviderHealth
from app.onboarding.detection import StartupDecision
from app.services.settings import SettingsService
from app.wiring import Wiring, build_wiring
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import CountingKeyring
from tests.fakes.sdk_install import install_fake_sdk
from tests.setup_harness import complete_setup

pytestmark = pytest.mark.integration

# The saved-sign-in kinds, taken from the settings type so no literal looks like a key
# assignment to secret scanners (no allowlist, ADR-031).
SAVED_TOKEN_KIND, SAVED_API_KEY_KIND = get_args(SavedSignInKind)

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
PORT = 8000
BASE_URL = f"http://127.0.0.1:{PORT}"  # must pass the host check (spec 2.9 rule 1)
ALLOW_FAKES = {"JOBPIPELINE_ALLOW_FAKE_PROVIDERS": "1"}
LEFTOVER = "jobpipeline-ai-x"
TOKEN_UNSAVED = AIProviderSettings(kind="claude_subscription_token")
TOKEN_SAVED = AIProviderSettings(
    kind="claude_subscription_token", saved_sign_ins=(SAVED_TOKEN_KIND,)
)
GET_PATHS = ("/", "/inbox", "/settings", "/health", "/setup", "/setup/welcome", "/setup/provider")


# --- helpers -------------------------------------------------------------------------------


def _config(
    tmp_path: Path, source: str, *, data_dir: Path | None = None, demo: bool = False
) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=data_dir if data_dir is not None else tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=PORT,
        log_level="INFO",
        demo=demo,
        setup=False,
        no_browser=False,
        data_dir_source=source,  # type: ignore[arg-type]
    )


def _wiring(
    environ: dict[str, str] | None = None, backend: CountingKeyring | None = None
) -> Wiring:
    return build_wiring(
        environ={} if environ is None else environ,
        demo=False,
        redactor=Redactor(),
        keyring_backend=backend if backend is not None else CountingKeyring(),
    )


@contextmanager
def running(config: RuntimeConfig, wiring: Wiring) -> Iterator[tuple[FastAPI, TestClient]]:
    app = create_app(config, clock=FrozenClock(START), wiring=wiring)
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        yield app, client


def _leftover(data_dir: Path) -> Path:
    folder = data_dir / "tmp" / "ai" / LEFTOVER
    (folder / "work").mkdir(parents=True)
    (folder / "system-prompt.md").write_text("Fictional leftover prompt.", encoding="utf-8")
    return folder


def count_health_calls(monkeypatch: pytest.MonkeyPatch) -> list[AIProviderSettings]:
    """Record every ``Wiring.provider_health`` call (the real one still runs)."""
    original = Wiring.provider_health
    calls: list[AIProviderSettings] = []

    def recording(self: Wiring, settings: AIProviderSettings) -> Any:
        calls.append(settings)
        return original(self, settings)

    monkeypatch.setattr(Wiring, "provider_health", recording)
    return calls


@pytest.fixture
def bundled_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The SDK looks installed with its bundled CLI on every OS (never run)."""
    return install_fake_sdk(monkeypatch, tmp_path / "sdk", with_cli=True)


# --- tests ---------------------------------------------------------------------------------


def test_create_app_accepts_injected_wiring(tmp_path: Path) -> None:
    backend = CountingKeyring()
    wiring = _wiring(ALLOW_FAKES, backend)
    complete_setup(tmp_path / "data", tmp_path / "cfg", clock=FrozenClock(START))

    with running(_config(tmp_path, "bootstrap"), wiring) as (app, client):
        assert app.state.wiring is wiring
        assert client.get("/health").status_code == 200

    assert backend.calls == 0, "start-up reads nothing from the keychain (ADR-046, ADR-047)"


def test_create_app_default_wiring_follows_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBPIPELINE_ALLOW_FAKE_PROVIDERS", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "FictionalProcessToken-0006-notreal")

    normal_app = create_app(_config(tmp_path / "a", "cli"), clock=FrozenClock(START))
    demo_app = create_app(_config(tmp_path / "b", "demo", demo=True), clock=FrozenClock(START))

    with TestClient(normal_app, base_url=BASE_URL), TestClient(demo_app, base_url=BASE_URL):
        normal = normal_app.state.wiring
        demo = demo_app.state.wiring
        assert isinstance(normal, Wiring) and isinstance(demo, Wiring)
        assert normal.allow_fakes is True
        assert "fake" in [choice.kind for choice in normal.provider_choices()]
        assert normal.secrets.lookup("claude_code_oauth_token").source == "environment"
        assert demo.secrets.get("claude_code_oauth_token") is None, (
            "demo mode never picks up the environment's credentials"
        )


def test_startup_health_feeds_the_banner_decision(tmp_path: Path, bundled_cli: Path) -> None:
    """AC-M1.1 case 4: a finished setup with no saved sign-in opens the app with a banner."""
    broken = tmp_path / "broken"
    complete_setup(
        broken / "data", broken / "cfg", clock=FrozenClock(START), provider=TOKEN_UNSAVED
    )
    backend = CountingKeyring()

    with running(_config(broken, "bootstrap"), _wiring({}, backend)) as (app, client):
        assert app.state.provider_health == ProviderHealth("missing_sign_in")
        assert app.state.onboarding.decision == StartupDecision(
            "app_with_banner", "provider", banners=("provider",)
        )
        page = client.get("/")
        assert page.status_code == 200, "a broken provider is a banner, never the wizard"
    assert backend.calls == 0

    healthy = tmp_path / "healthy"
    complete_setup(
        healthy / "data", healthy / "cfg", clock=FrozenClock(START), provider=TOKEN_SAVED
    )

    with running(_config(healthy, "bootstrap"), _wiring({})) as (app, _client):
        assert app.state.provider_health == ProviderHealth("ok")
        assert app.state.onboarding.decision == StartupDecision("app")


def test_startup_health_with_unallowed_fake_is_a_banner(tmp_path: Path) -> None:
    """A stored fake provider without the flag is reported as broken, not a crash (ADR-054)."""
    complete_setup(tmp_path / "data", tmp_path / "cfg", clock=FrozenClock(START))

    with running(_config(tmp_path, "bootstrap"), _wiring({})) as (app, _client):
        assert app.state.provider_health == ProviderHealth("not_allowed")
        assert app.state.onboarding.decision.kind == "app_with_banner"

    with running(_config(tmp_path, "bootstrap"), _wiring(ALLOW_FAKES)) as (app, _client):
        assert app.state.provider_health == ProviderHealth("ok")
        assert app.state.onboarding.decision == StartupDecision("app")


def test_startup_clears_ai_temp(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    complete_setup(data_dir, tmp_path / "cfg", clock=FrozenClock(START))
    leftover = _leftover(data_dir)
    unrelated = data_dir / "tmp" / "ai" / "notes.txt"
    unrelated.write_text("fictional", encoding="utf-8")

    with running(_config(tmp_path, "bootstrap"), _wiring(ALLOW_FAKES)):
        assert not leftover.exists()
        assert (data_dir / "tmp" / "ai").is_dir()
        assert unrelated.is_file()


def test_open_workspace_computes_health_and_clears_ai_temp(
    tmp_path: Path, bundled_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bootstrap config naming a folder that is gone: the app starts without a workspace.
    moved = tmp_path / "moved"
    write_bootstrap(tmp_path / "cfg", data_dir=moved, version=__version__)
    # The folder chosen in step 1: a finished setup with no saved sign-in and a leftover call.
    chosen = tmp_path / "chosen"
    complete_setup(
        chosen, tmp_path / "cfg-chosen", clock=FrozenClock(START), provider=TOKEN_UNSAVED
    )
    leftover = _leftover(chosen)
    calls = count_health_calls(monkeypatch)
    backend = CountingKeyring()

    with running(_config(tmp_path, "bootstrap", data_dir=moved), _wiring({}, backend)) as (
        app,
        _client,
    ):
        assert app.state.workspace is None
        assert leftover.exists(), "nothing is cleared before the folder is opened"
        before = len(calls)

        app.state.open_workspace(chosen)

        assert not leftover.exists()
        assert (chosen / "tmp" / "ai").is_dir()
        assert calls[before:] == [TOKEN_UNSAVED], "health from the opened folder's settings"
        assert app.state.provider_health == ProviderHealth("missing_sign_in")
        assert app.state.onboarding.decision == StartupDecision(
            "app_with_banner", "provider", banners=("provider",)
        )
    assert backend.calls == 0


def test_get_requests_never_recompute_provider_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete_setup(tmp_path / "data", tmp_path / "cfg", clock=FrozenClock(START))
    calls = count_health_calls(monkeypatch)
    backend = CountingKeyring()

    with running(_config(tmp_path, "bootstrap"), _wiring(ALLOW_FAKES, backend)) as (app, client):
        assert len(calls) == 1, "computed once at start-up"
        health = app.state.provider_health
        token = app.state.launch_token
        for path in GET_PATHS:
            client.get(path)
            client.get(path, headers={TOKEN_HEADER: token, "HX-Request": "true"})
            client.head(path)
        assert len(calls) == 1, "GET and HEAD requests never recompute provider health"
        assert app.state.provider_health is health
    assert backend.calls == 0, "no request read the keychain"


def test_refresh_provider_health_follows_saved_settings(
    tmp_path: Path, bundled_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one refresh operation state-changing requests use (M1.17): it reloads the stored
    provider, recomputes health and the setup decision, and reads no keychain item."""
    complete_setup(
        tmp_path / "data", tmp_path / "cfg", clock=FrozenClock(START), provider=TOKEN_SAVED
    )
    backend = CountingKeyring()

    with running(_config(tmp_path, "bootstrap"), _wiring({}, backend)) as (app, _client):
        assert app.state.provider_health == ProviderHealth("ok")
        assert app.state.onboarding.decision == StartupDecision("app")
        settings = SettingsService(app.state.workspace.db, FrozenClock(START))
        calls = count_health_calls(monkeypatch)

        stored = settings.load(AI_PROVIDER)
        settings.save(AI_PROVIDER, TOKEN_UNSAVED, expected_version=stored.version)
        assert app.state.provider_health == ProviderHealth("ok"), "nothing recomputes by itself"

        health = app.state.refresh_provider_health()

        assert calls == [TOKEN_UNSAVED]
        assert health == ProviderHealth("missing_sign_in")
        assert app.state.provider_health == health
        assert app.state.onboarding.decision == StartupDecision(
            "app_with_banner", "provider", banners=("provider",)
        )

        stored = settings.load(AI_PROVIDER)
        settings.save(AI_PROVIDER, TOKEN_SAVED, expected_version=stored.version)
        assert app.state.refresh_provider_health() == ProviderHealth("ok")
        assert app.state.onboarding.decision == StartupDecision("app")
    assert backend.calls == 0


def test_refresh_provider_health_needs_an_open_workspace(tmp_path: Path) -> None:
    moved = tmp_path / "moved"
    write_bootstrap(tmp_path / "cfg", data_dir=moved, version=__version__)

    with running(_config(tmp_path, "bootstrap", data_dir=moved), _wiring({})) as (app, _client):
        assert app.state.workspace is None
        assert app.state.provider_health is None
        with pytest.raises(RuntimeError):
            app.state.refresh_provider_health()


def test_startup_refuses_a_linked_ai_temp_folder(tmp_path: Path) -> None:
    """A link in place of <data>/tmp makes start-up fail with DataFolderUnavailable."""
    data_dir = tmp_path / "data"
    complete_setup(data_dir, tmp_path / "cfg", clock=FrozenClock(START))
    outside = tmp_path / "outside"
    (outside / "ai" / "jobpipeline-ai-precious").mkdir(parents=True)
    try:
        (data_dir / "tmp").symlink_to(outside, target_is_directory=True)
    except OSError:  # Windows without symlink rights
        pytest.skip("this system refuses to create symbolic links")

    app = create_app(_config(tmp_path, "bootstrap"), clock=FrozenClock(START), wiring=_wiring())
    with pytest.raises(DataFolderUnavailable), TestClient(app, base_url=BASE_URL):
        pass
    assert (outside / "ai" / "jobpipeline-ai-precious").is_dir()
