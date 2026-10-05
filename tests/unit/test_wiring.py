"""The composition root: secret store, provider factory, fake gating and provider health (M1.15).

Spec 2.3 rule 7 (providers are chosen only in ``app/wiring.py``), 2.9.9 (secrets only through
the redacting store), 2.9.12 (no secret reads on GET), 4.2; AC-M1.1 case 4 (the input to the
broken-provider banner) and AC-M1.3 (the fake chosen by a fictional key); ADR-044 items 2, 7
and 13, ADR-046 (no keychain read at start-up), ADR-047 (store composition and demo rules),
ADR-054 (fake gating), ADR-056 (provider defaults per tier), ADR-057 (``temp_root``) and
ADR-058 (CLI lookup).

Every wiring here is built from an explicit ``environ`` mapping and a counting in-memory
keyring backend, so the process environment and the real keychain are never involved. The
Claude SDK's bundled CLI is faked under ``tmp_path`` (``tests/fakes/sdk_install.py``) and is
never run. Every value is fictional.
"""

import asyncio
import inspect
import logging
import os
import socket
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, get_args

import pytest

from app.domain.errors import InvalidModelOutput
from app.domain.redaction import REDACTED, Redactor
from app.domain.settings import AIProviderSettings, ProviderKind, SavedSignInKind
from app.modules.ai import AIRequest, ProviderChoice, ProviderHealth
from app.modules.ai.executor import TaskExecutor
from app.modules.ai.prompts import PromptLibrary
from app.modules.ai.providers.claude_sdk import (
    CLAUDE_FAST_DEFAULT,
    CLAUDE_THOROUGH_DEFAULT,
    ClaudeSdkProvider,
)
from app.modules.ai.providers.fake import FAKE_FAST_MODEL, FAKE_THOROUGH_MODEL, FakeAIProvider
from app.modules.secrets import ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN
from app.modules.secrets.providers.chained import ChainedSecretStore
from app.wiring import AI_TEMP_FOLDER, Wiring, build_wiring, clear_ai_temp
from tests.fakes.keyring_backends import CountingKeyring
from tests.fakes.sdk_install import install_fake_sdk

# The saved-sign-in kinds, taken from the settings type so no literal looks like a key
# assignment to secret scanners (no allowlist, ADR-031).
SAVED_TOKEN_KIND, SAVED_API_KEY_KIND = get_args(SavedSignInKind)

ALLOW_VARIABLE = "JOBPIPELINE_ALLOW_FAKE_PROVIDERS"
SERVICE = "jobpipeline"
DEMO_SERVICE = "jobpipeline-demo"
TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 - a variable name, not a secret
API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
# Not shaped like real Anthropic credentials, so only registration can redact them.
ENV_TOKEN = "FictionalEnvToken-0001-notreal"  # noqa: S105 - fictional
ENV_API_KEY = "FictionalEnvApiKey-0002-notreal"
KEYCHAIN_TOKEN = "FictionalKeychainToken-0003-notreal"  # noqa: S105 - fictional
WRITTEN_TOKEN = "FictionalWrittenToken-0004-notreal"  # noqa: S105 - fictional
CLAUDE_KINDS: tuple[ProviderKind, ...] = (
    "claude_subscription_token",
    "claude_existing_login",
    "claude_api_key",
)
MODE_BY_KIND = {
    "claude_subscription_token": "subscription_token",
    "claude_existing_login": "existing_login",
    "claude_api_key": "api_key",
}
FAKE_LABEL = "Fake (testing only)"
REPO_ROOT = Path(__file__).resolve().parents[2]


class _NotCancelled:
    @property
    def cancelled(self) -> bool:
        return False


NOT_CANCELLED = _NotCancelled()


# --- helpers -------------------------------------------------------------------------------


def wiring_for(
    environ: Mapping[str, str] | None = None,
    *,
    demo: bool = False,
    redactor: Redactor | None = None,
    backend: CountingKeyring | None = None,
) -> Wiring:
    return build_wiring(
        environ={} if environ is None else environ,
        demo=demo,
        redactor=redactor if redactor is not None else Redactor(),
        keyring_backend=backend if backend is not None else CountingKeyring(),
    )


def allowed(**extra: str) -> dict[str, str]:
    return {ALLOW_VARIABLE: "1", **extra}


def kinds(wiring: Wiring) -> list[str]:
    return [choice.kind for choice in wiring.provider_choices()]


def record_init(monkeypatch: pytest.MonkeyPatch, cls: type) -> list[dict[str, Any]]:
    """Record the arguments of every ``cls(...)`` construction, by parameter name."""
    original = cls.__init__  # type: ignore[misc]
    signature = inspect.signature(original)
    calls: list[dict[str, Any]] = []

    def recording(self: Any, *args: Any, **kwargs: Any) -> None:
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        calls.append({name: value for name, value in bound.arguments.items() if name != "self"})
        original(self, *args, **kwargs)

    monkeypatch.setattr(cls, "__init__", recording)
    return calls


@pytest.fixture
def bundled_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The SDK looks installed with its bundled CLI (a placeholder that is never run)."""
    return install_fake_sdk(monkeypatch, tmp_path / "sdk-with-cli", with_cli=True)


@pytest.fixture
def no_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No bundled CLI, and (for Windows) no native claude.exe on PATH or in the profile."""
    install_fake_sdk(monkeypatch, tmp_path / "sdk-without-cli", with_cli=False)


@pytest.fixture
def no_side_effects(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Fail on any process start or network connection; yields the attempts made."""
    attempts: list[str] = []

    def refuse(name: str) -> Callable[..., Any]:
        def refused(*args: Any, **kwargs: Any) -> Any:
            attempts.append(name)
            raise AssertionError(f"provider health must not call {name}")

        return refused

    monkeypatch.setattr(subprocess, "Popen", refuse("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "run", refuse("subprocess.run"))
    monkeypatch.setattr(socket, "create_connection", refuse("socket.create_connection"))
    monkeypatch.setattr(socket.socket, "connect", refuse("socket.connect"))
    monkeypatch.setattr(
        "app.modules.ai.providers.claude_sdk.probe_cli_version", refuse("probe_cli_version")
    )
    yield attempts


def _request(task: str) -> AIRequest:
    return AIRequest(
        task=task,
        prompt_id=task,
        prompt_version="1",
        system="Fictional system text.",
        user="Fictional user text.",
        output_schema={"type": "object"},
        model=FAKE_FAST_MODEL,
    )


# --- building providers ----------------------------------------------------------------------


@pytest.mark.parametrize("kind", CLAUDE_KINDS)
def test_wiring_builds_claude_provider_for_each_mode(
    kind: ProviderKind, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = CountingKeyring()
    wiring = wiring_for(allowed(), backend=backend)
    built = record_init(monkeypatch, ClaudeSdkProvider)
    data_dir = tmp_path / "data"

    provider = wiring.ai_provider(AIProviderSettings(kind=kind), data_dir=data_dir)

    assert isinstance(provider, ClaudeSdkProvider)
    assert provider.name == "claude"
    assert len(built) == 1
    arguments = built[0]
    assert arguments["mode"] == MODE_BY_KIND[kind]
    assert arguments["secrets"] is wiring.secrets, "only the redacting chained store is injected"
    assert isinstance(arguments["secrets"], ChainedSecretStore)
    assert arguments["temp_root"] == data_dir / "tmp" / "ai", "calls run under <data>/tmp/ai"
    assert backend.calls == 0, "building a provider reads nothing from the keychain"


def test_ai_provider_refuses_none_and_unallowed_fake(tmp_path: Path) -> None:
    wiring = wiring_for({})

    with pytest.raises(ValueError):
        wiring.ai_provider(AIProviderSettings(), data_dir=tmp_path)
    with pytest.raises(ValueError):
        wiring.ai_provider(AIProviderSettings(kind="fake"), data_dir=tmp_path)

    # The flag decides, not the stored kind: with it, the same setting builds the fake.
    assert isinstance(
        wiring_for(allowed()).ai_provider(AIProviderSettings(kind="fake"), data_dir=tmp_path),
        FakeAIProvider,
    )
    with pytest.raises(ValueError):
        wiring_for(allowed()).ai_provider(AIProviderSettings(), data_dir=tmp_path)


# --- fake gating (ADR-044 item 2, ADR-054) ----------------------------------------------------


@pytest.mark.parametrize(
    "environ",
    [{}, {ALLOW_VARIABLE: "true"}, {ALLOW_VARIABLE: "0"}, {ALLOW_VARIABLE: "yes"}],
    ids=["unset", "true", "0", "yes"],
)
def test_fake_provider_refused_without_allow_env(environ: dict[str, str], tmp_path: Path) -> None:
    backend = CountingKeyring()
    wiring = wiring_for(environ, backend=backend)

    assert wiring.allow_fakes is False
    assert "fake" not in kinds(wiring)
    assert all("fake" not in choice.label.lower() for choice in wiring.provider_choices())
    with pytest.raises(ValueError):
        wiring.ai_provider(AIProviderSettings(kind="fake"), data_dir=tmp_path)
    with pytest.raises(ValueError):
        wiring.fake_for_key("fake-connected-fictional-0001")
    # A stored fake provider without the flag is a broken provider, not a crash (ADR-054).
    assert wiring.provider_health(AIProviderSettings(kind="fake")) == ProviderHealth("not_allowed")
    assert backend.calls == 0


@pytest.mark.parametrize("value", [" 1", "1 ", "01", "1\n", "TRUE"], ids=repr)
def test_fake_provider_flag_must_be_exactly_one(value: str) -> None:
    wiring = wiring_for({ALLOW_VARIABLE: value})

    assert wiring.allow_fakes is False
    assert "fake" not in kinds(wiring)


async def test_fake_provider_selectable_with_allow_env(tmp_path: Path) -> None:
    backend = CountingKeyring()
    redactor = Redactor()
    wiring = wiring_for(allowed(), backend=backend, redactor=redactor)

    assert wiring.allow_fakes is True
    assert ProviderChoice("fake", FAKE_LABEL) in wiring.provider_choices()
    assert kinds(wiring).count("fake") == 1

    provider = wiring.ai_provider(AIProviderSettings(kind="fake"), data_dir=tmp_path)
    assert isinstance(provider, FakeAIProvider)
    # App mode: an unscripted task is a plain error, not a crash, and no request is kept.
    with pytest.raises(InvalidModelOutput):
        await provider.run(_request("fictional_unscripted_task"), NOT_CANCELLED)
    assert provider.requests == []

    # The connection outcome comes from fictional keywords in the pasted key (AC-M1.3).
    expected = {
        "pasted-fake-connected-0001": True,
        "pasted-fake-auth-rejected-0002": False,
        "pasted-fake-not-installed-0003": False,
        "pasted-fake-usage-limit-0004": False,
        "pasted-fake-offline-0005": False,
        "Fictional-Unknown-Key-0006": False,
    }
    messages: dict[str, str] = {}
    for key, ok in expected.items():
        fake = wiring.fake_for_key(key)
        assert isinstance(fake, FakeAIProvider)
        result = await fake.test_connection(FAKE_FAST_MODEL)
        assert result.ok is ok, key
        messages[key] = result.message
        # Nothing keeps the key: not the provider, not the keychain.
        assert key not in repr(vars(fake)), key
        with pytest.raises(InvalidModelOutput):
            await fake.run(_request("fictional_unscripted_task"), NOT_CANCELLED)
        assert fake.requests == []
    assert messages["pasted-fake-connected-0001"] == "Connected"
    assert len(set(messages.values())) == 5, "each failure has its own mapped message"
    assert messages["Fictional-Unknown-Key-0006"] == messages["pasted-fake-auth-rejected-0002"]
    assert (await wiring.fake_for_key(None).test_connection(FAKE_FAST_MODEL)).ok is False
    assert backend.writes == 0 and backend.items == {}, "a fake key is never stored"


def test_provider_choices_hide_fake_without_env() -> None:
    choices = wiring_for({}).provider_choices()

    assert [choice.kind for choice in choices] == list(CLAUDE_KINDS)
    labels = [choice.label for choice in choices]
    assert len(set(labels)) == len(labels)
    for label in labels:
        assert label and label == label.strip()
        assert "fake" not in label.lower() and "testing" not in label.lower()
    assert all(isinstance(choice, ProviderChoice) for choice in choices)

    with_fakes = wiring_for(allowed()).provider_choices()
    assert list(with_fakes[: len(choices)]) == list(choices), "the Claude choices are the same"
    assert [choice.kind for choice in with_fakes] == [*CLAUDE_KINDS, "fake"]


# --- the secret store (ADR-047) ---------------------------------------------------------------


def test_secret_store_is_redacting_chained_keyring_env() -> None:
    backend = CountingKeyring()
    redactor = Redactor()
    environ = {TOKEN_VARIABLE: ENV_TOKEN, API_KEY_VARIABLE: f"  {ENV_API_KEY}  "}

    wiring = build_wiring(environ=environ, demo=False, redactor=redactor, keyring_backend=backend)

    # Building reads no keychain item, not even the availability probe (ADR-046).
    assert backend.calls == 0
    # Environment secrets are registered at build time, before anything reads them.
    assert redactor.redact(f"leak {ENV_TOKEN}") == f"leak {REDACTED}"
    assert redactor.redact(f"leak {ENV_API_KEY}") == f"leak {REDACTED}"
    assert wiring.redactor is redactor
    secrets = wiring.secrets
    assert isinstance(secrets, ChainedSecretStore)

    # Environment fallback while the keychain has nothing.
    assert secrets.lookup(CLAUDE_CODE_OAUTH_TOKEN).source == "environment"
    assert secrets.get(CLAUDE_CODE_OAUTH_TOKEN) == ENV_TOKEN
    assert secrets.get(ANTHROPIC_API_KEY) == ENV_API_KEY

    # The keychain wins, under the "jobpipeline" service; a value read is registered.
    backend.items[(SERVICE, CLAUDE_CODE_OAUTH_TOKEN)] = KEYCHAIN_TOKEN
    assert redactor.redact(KEYCHAIN_TOKEN) == KEYCHAIN_TOKEN, "not registered before the read"
    found = secrets.lookup(CLAUDE_CODE_OAUTH_TOKEN)
    assert (found.value, found.source) == (KEYCHAIN_TOKEN, "keychain")
    assert redactor.redact(KEYCHAIN_TOKEN) == REDACTED

    # Writes go to the keychain only, and the value is registered.
    secrets.set(ANTHROPIC_API_KEY, WRITTEN_TOKEN)
    assert backend.items[(SERVICE, ANTHROPIC_API_KEY)] == WRITTEN_TOKEN
    assert redactor.redact(WRITTEN_TOKEN) == REDACTED
    assert environ[API_KEY_VARIABLE] == f"  {ENV_API_KEY}  ", "the environment is never written"


def test_demo_wiring_uses_demo_keychain_and_ignores_environment(bundled_cli: Path) -> None:
    backend = CountingKeyring()
    redactor = Redactor()
    environ = allowed(**{TOKEN_VARIABLE: ENV_TOKEN, API_KEY_VARIABLE: ENV_API_KEY})
    backend.items[(SERVICE, CLAUDE_CODE_OAUTH_TOKEN)] = KEYCHAIN_TOKEN  # the real profile's

    wiring = build_wiring(environ=environ, demo=True, redactor=redactor, keyring_backend=backend)

    assert backend.calls == 0
    # Demo runs never pick up real credentials: neither the environment...
    assert redactor.redact(ENV_TOKEN) == ENV_TOKEN, "demo registers no environment secret"
    assert wiring.secrets.get(ANTHROPIC_API_KEY) is None
    assert wiring.provider_health(
        AIProviderSettings(kind="claude_subscription_token")
    ) == ProviderHealth("missing_sign_in")
    # ...nor the normal keychain service.
    assert wiring.secrets.get(CLAUDE_CODE_OAUTH_TOKEN) is None
    wiring.secrets.set(CLAUDE_CODE_OAUTH_TOKEN, WRITTEN_TOKEN)
    assert backend.items[(DEMO_SERVICE, CLAUDE_CODE_OAUTH_TOKEN)] == WRITTEN_TOKEN
    assert backend.items[(SERVICE, CLAUDE_CODE_OAUTH_TOKEN)] == KEYCHAIN_TOKEN
    assert wiring.secrets.get(CLAUDE_CODE_OAUTH_TOKEN) == WRITTEN_TOKEN
    # The allow flag is still read from the environment in demo mode.
    assert wiring.allow_fakes is True


# --- provider health (AC-M1.1 case 4; spec 2.9.12; ADR-046) ----------------------------------


def test_provider_health_missing_secret_is_broken_without_network_call(
    bundled_cli: Path, no_side_effects: list[str]
) -> None:
    backend = CountingKeyring()
    backend.items[(SERVICE, CLAUDE_CODE_OAUTH_TOKEN)] = KEYCHAIN_TOKEN  # health must not look
    wiring = wiring_for({}, backend=backend)

    health = wiring.provider_health(AIProviderSettings(kind="claude_subscription_token"))

    assert health == ProviderHealth("missing_sign_in")
    assert health.ok is False
    assert backend.calls == 0, "health records only the saved flag; it never reads the keychain"
    assert no_side_effects == [], "no CLI run and no network call"


HEALTH_CASES: list[tuple[str, dict[str, str], AIProviderSettings, str]] = [
    ("not_configured", {}, AIProviderSettings(), "not_configured"),
    ("not_configured_with_flag", allowed(), AIProviderSettings(), "not_configured"),
    ("fake_not_allowed", {}, AIProviderSettings(kind="fake"), "not_allowed"),
    ("fake_allowed", allowed(), AIProviderSettings(kind="fake"), "ok"),
    (
        "token_saved",
        {},
        AIProviderSettings(kind="claude_subscription_token", sign_in_saved_for=SAVED_TOKEN_KIND),
        "ok",
    ),
    (
        "api_key_saved",
        {},
        AIProviderSettings(kind="claude_api_key", sign_in_saved_for=SAVED_API_KEY_KIND),
        "ok",
    ),
    (
        "token_from_env",
        {TOKEN_VARIABLE: ENV_TOKEN},
        AIProviderSettings(kind="claude_subscription_token"),
        "ok",
    ),
    (
        "api_key_from_env",
        {API_KEY_VARIABLE: ENV_API_KEY},
        AIProviderSettings(kind="claude_api_key"),
        "ok",
    ),
    (
        "token_mode_ignores_api_key_env",
        {API_KEY_VARIABLE: ENV_API_KEY},
        AIProviderSettings(kind="claude_subscription_token"),
        "missing_sign_in",
    ),
    (
        "api_key_mode_ignores_token_env",
        {TOKEN_VARIABLE: ENV_TOKEN},
        AIProviderSettings(kind="claude_api_key"),
        "missing_sign_in",
    ),
    (
        "blank_env_is_not_a_sign_in",
        {TOKEN_VARIABLE: "   "},
        AIProviderSettings(kind="claude_subscription_token"),
        "missing_sign_in",
    ),
    ("api_key_missing", {}, AIProviderSettings(kind="claude_api_key"), "missing_sign_in"),
    ("existing_login", {}, AIProviderSettings(kind="claude_existing_login"), "ok"),
]


@pytest.mark.parametrize(
    ("environ", "settings", "expected"),
    [case[1:] for case in HEALTH_CASES],
    ids=[case[0] for case in HEALTH_CASES],
)
def test_provider_health_statuses(
    environ: dict[str, str],
    settings: AIProviderSettings,
    expected: str,
    bundled_cli: Path,
    no_side_effects: list[str],
) -> None:
    backend = CountingKeyring()
    wiring = wiring_for(environ, backend=backend)

    health = wiring.provider_health(settings)

    assert health.status == expected
    assert health.ok is (expected == "ok")
    assert backend.calls == 0
    assert no_side_effects == []


@pytest.mark.parametrize(
    "settings",
    [
        AIProviderSettings(kind="claude_existing_login"),
        AIProviderSettings(kind="claude_subscription_token", sign_in_saved_for=SAVED_TOKEN_KIND),
        AIProviderSettings(kind="claude_api_key", sign_in_saved_for=SAVED_API_KEY_KIND),
    ],
    ids=["existing_login", "token_saved", "api_key_saved"],
)
def test_provider_health_cli_missing(
    settings: AIProviderSettings, no_cli: None, no_side_effects: list[str]
) -> None:
    backend = CountingKeyring()
    wiring = wiring_for({}, backend=backend)

    health = wiring.provider_health(settings)

    assert health == ProviderHealth("cli_missing")
    assert health.ok is False
    assert backend.calls == 0
    assert no_side_effects == [], "no version probe and no network"


def test_provider_health_fake_needs_no_cli(no_cli: None, no_side_effects: list[str]) -> None:
    assert wiring_for(allowed()).provider_health(AIProviderSettings(kind="fake")).ok is True


def test_provider_health_reads_the_environment_at_call_time(bundled_cli: Path) -> None:
    environ: dict[str, str] = {}
    wiring = wiring_for(environ)
    settings = AIProviderSettings(kind="claude_api_key")

    assert wiring.provider_health(settings).status == "missing_sign_in"
    environ[API_KEY_VARIABLE] = ENV_API_KEY
    assert wiring.provider_health(settings).status == "ok"


# --- the task runner (ADR-044 item 13, ADR-056) -----------------------------------------------


@pytest.mark.parametrize(
    ("kind", "defaults"),
    [
        (
            "claude_subscription_token",
            {"fast": CLAUDE_FAST_DEFAULT, "thorough": CLAUDE_THOROUGH_DEFAULT},
        ),
        (
            "claude_existing_login",
            {"fast": CLAUDE_FAST_DEFAULT, "thorough": CLAUDE_THOROUGH_DEFAULT},
        ),
        ("claude_api_key", {"fast": CLAUDE_FAST_DEFAULT, "thorough": CLAUDE_THOROUGH_DEFAULT}),
        ("fake", {"fast": FAKE_FAST_MODEL, "thorough": FAKE_THOROUGH_MODEL}),
    ],
)
def test_task_runner_uses_provider_defaults_and_overrides_folder(
    kind: ProviderKind,
    defaults: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiring = wiring_for(allowed())
    stored = AIProviderSettings(kind=kind)
    # The defaults follow the provider itself (its name), not the stored kind.
    provider = wiring.ai_provider(stored, data_dir=tmp_path / "data")

    def settings() -> AIProviderSettings:
        return stored

    executors = record_init(monkeypatch, TaskExecutor)
    libraries = record_init(monkeypatch, PromptLibrary)
    data_dir = tmp_path / "data"

    runner = wiring.task_runner(provider, settings, data_dir=data_dir)

    assert isinstance(runner, TaskExecutor)
    assert len(executors) == 1 and len(libraries) == 1
    executor = executors[0]
    assert executor["provider"] is provider
    assert executor["settings"] is settings, "settings are read at run time, not copied"
    assert dict(executor["provider_defaults"]) == defaults
    library = libraries[0]
    assert Path(library["defaults_dir"]).resolve() == (REPO_ROOT / "prompts").resolve()
    assert library["overrides_dir"] == data_dir / "prompts" / "overrides"
    assert isinstance(executor["prompts"], PromptLibrary)
    assert not (data_dir / "prompts").exists(), "building the runner creates nothing"


class _UnknownProvider(FakeAIProvider):
    name = "fictional-unknown"  # type: ignore[misc]


def test_task_runner_refuses_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        wiring_for(allowed()).task_runner(
            _UnknownProvider(app_mode=True), AIProviderSettings, data_dir=tmp_path
        )


# --- the AI temp folder (ADR-057) -------------------------------------------------------------


def test_clear_ai_temp_removes_only_leftover_call_folders(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    assert AI_TEMP_FOLDER == ("tmp", "ai")

    created = clear_ai_temp(data_dir)

    folder = data_dir / "tmp" / "ai"
    assert created == folder
    assert folder.is_dir()
    if os.name == "posix":
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700

    leftover = folder / "jobpipeline-ai-abc123"
    (leftover / "work").mkdir(parents=True)
    (leftover / "system-prompt.md").write_text("Fictional leftover prompt.", encoding="utf-8")
    (leftover / "config").mkdir()
    (folder / "jobpipeline-ai-x").mkdir()
    kept_file = folder / "notes.txt"
    kept_file.write_text("fictional", encoding="utf-8")
    kept_named_file = folder / "jobpipeline-ai-not-a-folder"
    kept_named_file.write_text("fictional", encoding="utf-8")
    kept_folder = folder / "other-folder"
    kept_folder.mkdir()
    (kept_folder / "inside.txt").write_text("fictional", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("fictional", encoding="utf-8")
    if os.name == "posix":
        (folder / "jobpipeline-ai-link").symlink_to(outside, target_is_directory=True)

    assert clear_ai_temp(data_dir) == folder

    assert not leftover.exists()
    assert not (folder / "jobpipeline-ai-x").exists()
    assert kept_file.read_text(encoding="utf-8") == "fictional"
    assert kept_named_file.is_file()
    assert (kept_folder / "inside.txt").is_file()
    assert (outside / "precious.txt").is_file(), "a link is never followed out of the folder"
    assert not (data_dir / "tmp" / "jobpipeline-ai-abc123").exists()


def test_clear_ai_temp_leaves_sibling_tmp_content(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    sibling = data_dir / "tmp" / "jobpipeline-ai-sibling"
    sibling.mkdir(parents=True)

    clear_ai_temp(data_dir)

    assert sibling.is_dir(), "only folders inside tmp/ai are cleared"


def test_wiring_module_is_the_only_provider_chooser() -> None:
    """Spec 2.3 rule 7: concrete providers are built in app/wiring.py, not in main or cli."""
    for name in ("main.py", "cli.py"):
        text = (REPO_ROOT / "app" / name).read_text(encoding="utf-8")
        assert "ClaudeSdkProvider(" not in text, name
        assert "FakeAIProvider(" not in text, name
        assert "KeyringSecretStore(" not in text, name
    wiring_text = (REPO_ROOT / "app" / "wiring.py").read_text(encoding="utf-8")
    assert "ClaudeSdkProvider(" in wiring_text
    assert "ChainedSecretStore(" in wiring_text


def test_fake_for_key_runs_offline_without_event_loop_leaks() -> None:
    """The fake chosen by a key answers through asyncio without any network or sleep."""
    fake = wiring_for(allowed()).fake_for_key("pasted-fake-offline-0001")
    result = asyncio.run(fake.test_connection(FAKE_FAST_MODEL))
    assert result.ok is False
    assert result.message


# --- M1.15 review decisions -------------------------------------------------------------------


FACTORY_CONFORMANCE = """\
from app.domain.redaction import Redactor
from app.modules.ai import AIProviderFactory
from app.wiring import build_wiring



factory: AIProviderFactory = build_wiring(environ={}, demo=False, redactor=Redactor())
"""


def test_wiring_satisfies_ai_provider_factory_protocol(tmp_path: Path) -> None:
    """Static conformance, checked with mypy --strict like the other protocol tests."""
    sample = tmp_path / "conformance.py"
    sample.write_text(FACTORY_CONFORMANCE, encoding="utf-8")
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


@pytest.mark.parametrize(
    ("kind", "saved_for"),
    [
        ("claude_subscription_token", "claude_api_key"),
        ("claude_api_key", "claude_subscription_token"),
    ],
)
def test_saved_sign_in_for_another_mode_is_missing(
    kind: ProviderKind, saved_for: Any, bundled_cli: Path, no_side_effects: list[str]
) -> None:
    """A credential saved for one sign-in route doesn't count after switching to another."""
    backend = CountingKeyring()
    wiring = wiring_for({}, backend=backend)

    health = wiring.provider_health(AIProviderSettings(kind=kind, sign_in_saved_for=saved_for))

    assert health == ProviderHealth("missing_sign_in")
    assert wiring.provider_health(
        AIProviderSettings(kind=kind, sign_in_saved_for=kind)
    ) == ProviderHealth("ok")
    assert backend.calls == 0


@pytest.mark.parametrize("kind", ["claude_subscription_token", "claude_api_key"])
def test_provider_health_reports_missing_cli_before_missing_sign_in(
    kind: ProviderKind, no_cli: None, no_side_effects: list[str]
) -> None:
    """Installing the CLI comes first, so the user isn't sent to fix the key first."""
    health = wiring_for({}).provider_health(AIProviderSettings(kind=kind))

    assert health == ProviderHealth("cli_missing")


def test_demo_never_offers_the_computers_own_sign_in(
    tmp_path: Path, bundled_cli: Path, no_side_effects: list[str]
) -> None:
    """ADR-044 item 7: demo runs never touch real credentials, including Claude Code's own
    sign-in on this computer."""
    backend = CountingKeyring()
    demo = wiring_for(allowed(), demo=True, backend=backend)
    existing = AIProviderSettings(kind="claude_existing_login")

    assert "claude_existing_login" not in kinds(demo)
    assert kinds(demo) == ["claude_subscription_token", "claude_api_key", "fake"]
    with pytest.raises(ValueError):
        demo.ai_provider(existing, data_dir=tmp_path)
    assert demo.provider_health(existing) == ProviderHealth("not_allowed")
    # The other routes still work in demo mode, with the demo keychain.
    saved = AIProviderSettings(kind="claude_api_key", sign_in_saved_for=SAVED_API_KEY_KIND)
    assert demo.provider_health(saved) == ProviderHealth("ok")
    assert isinstance(demo.ai_provider(saved, data_dir=tmp_path), ClaudeSdkProvider)
    assert backend.calls == 0

    normal = wiring_for(allowed())
    assert "claude_existing_login" in kinds(normal)
    assert normal.provider_health(existing) == ProviderHealth("ok")


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:  # Windows without symlink rights
        pytest.skip("this system refuses to create symbolic links")


@pytest.mark.parametrize("linked", ["tmp", "tmp/ai"])
def test_clear_ai_temp_refuses_linked_folders(tmp_path: Path, linked: str) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside = tmp_path / "outside"
    (outside / "ai" / "jobpipeline-ai-precious").mkdir(parents=True)
    (outside / "jobpipeline-ai-precious").mkdir()
    if linked == "tmp":
        _symlink_or_skip(data_dir / "tmp", outside)
    else:
        (data_dir / "tmp").mkdir()
        _symlink_or_skip(data_dir / "tmp" / "ai", outside)
    mode_before = stat.S_IMODE(outside.stat().st_mode)

    with pytest.raises(OSError):
        clear_ai_temp(data_dir)

    assert (outside / "ai" / "jobpipeline-ai-precious").is_dir()
    assert (outside / "jobpipeline-ai-precious").is_dir()
    assert stat.S_IMODE(outside.stat().st_mode) == mode_before, "the link target is untouched"


def test_clear_ai_temp_refuses_a_file_in_place_of_the_folder(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "tmp").mkdir(parents=True)
    (data_dir / "tmp" / "ai").write_text("fictional", encoding="utf-8")

    with pytest.raises(OSError):
        clear_ai_temp(data_dir)


def test_allowing_fakes_logs_one_warning_naming_only_the_variable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    environ = allowed(**{TOKEN_VARIABLE: ENV_TOKEN})

    wiring_for(environ)

    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert message == "JOBPIPELINE_ALLOW_FAKE_PROVIDERS is set: fake AI providers are offered"
    assert ENV_TOKEN not in caplog.text

    caplog.clear()
    wiring_for({})
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
