"""The composition root for providers (spec 2.3 rule 7, 2.9.9, 2.9.12; ADR-030).

``build_wiring`` builds the one secret store the app uses (keychain first, then the
environment, every value registered with the log redactor; ADR-047), decides once whether
fake providers may be chosen (``JOBPIPELINE_ALLOW_FAKE_PROVIDERS`` exactly ``"1"``; ADR-044
item 2, ADR-054) and returns a ``Wiring``, the app's ``AIProviderFactory``.

Building reads nothing from the keychain (ADR-046). ``provider_health`` is configuration
only: it never reads the keychain, runs the CLI or uses the network, so it can run when a
workspace opens and after a state-changing request; GET requests read the cached result
(spec 2.9.12). Demo runs never touch real credentials (ADR-044 item 7): the demo keychain
service, no environment secrets, and no use of the computer's own Claude Code sign-in.
Services and routes never import this module; they get what it builds injected.
"""

import logging
import os
import shutil
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from app.domain.errors import ProviderNotInstalled
from app.domain.redaction import Redactor
from app.domain.settings import AIProviderSettings, ProviderKind, Tier, claude_auth_mode
from app.domain.tiers import resolve_model
from app.modules.ai import (
    AIProvider,
    AITaskRunner,
    ModelChoice,
    ProviderChoice,
    ProviderHealth,
)
from app.modules.ai.executor import TaskExecutor
from app.modules.ai.prompts import PromptLibrary
from app.modules.ai.providers.claude_sdk import (
    CALL_FOLDER_PREFIX,
    CLAUDE_FAST_DEFAULT,
    CLAUDE_THOROUGH_DEFAULT,
    SECRET_KEY_BY_MODE,
    ClaudeSdkProvider,
    bundled_cli_path,
    windows_cli_candidates,
)
from app.modules.ai.providers.claude_sdk import KNOWN_MODELS as CLAUDE_MODELS
from app.modules.ai.providers.fake import (
    FAKE_FAST_MODEL,
    FAKE_THOROUGH_MODEL,
    FakeAIProvider,
    outcome_for_key,
)
from app.modules.ai.providers.fake import KNOWN_MODELS as FAKE_MODELS
from app.modules.secrets import KNOWN_SECRET_KEYS
from app.modules.secrets.providers.chained import ChainedSecretStore
from app.modules.secrets.providers.env import EnvSecretStore
from app.modules.secrets.providers.keyring_store import (
    DEMO_SERVICE_NAME,
    SERVICE_NAME,
    KeyringBackend,
    KeyringSecretStore,
)
from app.modules.secrets.providers.redacting import register_environment_secrets

__all__ = ["AI_TEMP_FOLDER", "Wiring", "build_wiring", "clear_ai_temp"]

ALLOW_FAKES_VARIABLE: Final = "JOBPIPELINE_ALLOW_FAKE_PROVIDERS"
AI_TEMP_FOLDER: Final = ("tmp", "ai")  # under the data folder (ADR-057)
PROMPTS_DIR: Final = Path(__file__).resolve().parent.parent / "prompts"
OVERRIDES_FOLDER: Final = ("prompts", "overrides")

_log = logging.getLogger(__name__)

_CLAUDE_CHOICES: Final[tuple[tuple[ProviderKind, str], ...]] = (
    ("claude_subscription_token", "Claude subscription (sign-in token)"),
    ("claude_existing_login", "Claude Code sign-in on this computer"),
    ("claude_api_key", "Anthropic API key"),
)
# The computer's own Claude Code sign-in is a real credential: never used in demo mode.
_NOT_IN_DEMO: Final[frozenset[ProviderKind]] = frozenset({"claude_existing_login"})
_FAKE_LABEL: Final = "Fake (testing only)"
_DEFAULTS_BY_PROVIDER: Final[Mapping[str, Mapping[Tier, str]]] = {
    ClaudeSdkProvider.name: {"fast": CLAUDE_FAST_DEFAULT, "thorough": CLAUDE_THOROUGH_DEFAULT},
    FakeAIProvider.name: {"fast": FAKE_FAST_MODEL, "thorough": FAKE_THOROUGH_MODEL},
}


_MODELS_BY_PROVIDER: Final[Mapping[str, tuple[ModelChoice, ...]]] = {
    ClaudeSdkProvider.name: CLAUDE_MODELS,
    FakeAIProvider.name: FAKE_MODELS,
}


class Wiring:
    """What the app builds providers with (an ``AIProviderFactory``). Built once per process
    by ``build_wiring``."""

    def __init__(
        self,
        *,
        redactor: Redactor,
        secrets: ChainedSecretStore,
        environment: EnvSecretStore,
        allow_fakes: bool,
        demo: bool,
    ) -> None:
        self.redactor = redactor
        self.secrets = secrets  # the only store injected anywhere (ADR-047)
        self._environment = environment
        self.allow_fakes = allow_fakes
        self.demo = demo
        # Names only, computed once: the setup guide shows which variable is in use (M1.17).
        self._environment_keys = frozenset(
            key for key in KNOWN_SECRET_KEYS if environment.get(key) is not None
        )

    def provider_choices(self) -> tuple[ProviderChoice, ...]:
        """The Claude sign-in routes (without the computer's own sign-in in demo mode), then
        the fake only when fakes are allowed."""
        choices = [
            ProviderChoice(kind, label)
            for kind, label in _CLAUDE_CHOICES
            if not (self.demo and kind in _NOT_IN_DEMO)
        ]
        if self.allow_fakes:
            choices.append(ProviderChoice("fake", _FAKE_LABEL))
        return tuple(choices)

    def ai_provider(self, settings: AIProviderSettings, *, data_dir: Path) -> AIProvider:
        """The configured provider; its calls use ``<data>/tmp/ai``. Reads no secret: the
        provider does, at call time.

        Raises ``ValueError`` when no provider is configured or the kind isn't on offer (a
        fake without the flag, the computer's own sign-in in demo mode). Routes check a
        chosen kind against ``provider_choices()`` first (M1.17).
        """
        kind = settings.kind
        if kind is None or kind not in self._offered():
            raise ValueError("This AI provider isn't available.")
        if kind == "fake":
            return FakeAIProvider(app_mode=True)
        mode = claude_auth_mode(kind)
        if mode is None:  # pragma: no cover - every other kind is a Claude kind
            raise ValueError("This AI provider isn't available.")
        return ClaudeSdkProvider(
            secrets=self.secrets, mode=mode, temp_root=data_dir.joinpath(*AI_TEMP_FOLDER)
        )

    def fake_for_key(self, key: str | None) -> FakeAIProvider:
        """The fake whose connection test follows the fictional keywords in ``key``. Only
        the outcome is kept, never the key (ADR-054).

        Raises ``ValueError`` while fakes aren't allowed; routes check the chosen kind
        against ``provider_choices()`` first (M1.17).
        """
        if not self.allow_fakes:
            raise ValueError("Fake providers are not allowed.")
        return FakeAIProvider(connection=outcome_for_key(key), app_mode=True)

    def provider_health(self, settings: AIProviderSettings) -> ProviderHealth:
        """Configuration only: whether the kind is on offer, whether a CLI file is present,
        then the kind's saved sign-in or its environment variable. No keychain read, no CLI
        run, no network (ADR-046; spec 2.9.12)."""
        kind = settings.kind
        if kind is None:
            return ProviderHealth("not_configured")
        if kind not in self._offered():
            return ProviderHealth("not_allowed")
        if kind == "fake":
            return ProviderHealth("ok")
        mode = claude_auth_mode(kind)
        if mode is None:  # pragma: no cover - every other kind is a Claude kind
            return ProviderHealth("not_configured")
        if not _cli_present():
            return ProviderHealth("cli_missing")
        key = SECRET_KEY_BY_MODE[mode]
        saved = kind in settings.saved_sign_ins
        if key is not None and not saved and self._environment.get(key) is None:
            return ProviderHealth("missing_sign_in")
        return ProviderHealth("ok")

    def connection_model(self, settings: AIProviderSettings) -> str:
        """The settings' Fast tier model, else the configured provider's Fast default (the
        Claude default when nothing is chosen yet)."""
        provider = _provider_name(settings.kind)
        return resolve_model("fast", settings, _DEFAULTS_BY_PROVIDER[provider])

    def known_models(self, kind: ProviderKind | None) -> tuple[ModelChoice, ...]:
        """The models offered by the provider ``kind`` stands for; builds nothing."""
        return _MODELS_BY_PROVIDER[_provider_name(kind)]

    def environment_keys(self) -> frozenset[str]:
        """The secret keys the environment provides, fixed at start when wiring was built
        (names only; always empty in demo mode)."""
        return self._environment_keys

    def task_runner(
        self,
        provider: AIProvider,
        settings: Callable[[], AIProviderSettings],
        *,
        data_dir: Path,
    ) -> AITaskRunner:
        """The task executor over ``provider``, with that provider's default model per tier;
        ``settings`` is read at run time. User prompt overrides live in
        ``<data>/prompts/overrides`` (nothing is created here). Raises ``ValueError`` for a
        provider wiring doesn't know."""
        defaults = _DEFAULTS_BY_PROVIDER.get(provider.name)
        if defaults is None:
            raise ValueError("Unknown AI provider.")
        prompts = PromptLibrary(
            defaults_dir=PROMPTS_DIR, overrides_dir=data_dir.joinpath(*OVERRIDES_FOLDER)
        )
        return TaskExecutor(
            provider=provider, prompts=prompts, settings=settings, provider_defaults=defaults
        )

    def _offered(self) -> frozenset[ProviderKind]:
        return frozenset(choice.kind for choice in self.provider_choices())


def build_wiring(
    *,
    environ: Mapping[str, str],
    demo: bool,
    redactor: Redactor,
    keyring_backend: KeyringBackend | None = None,
) -> Wiring:
    """Compose the secret store and read the allow flag (ADR-047, ADR-054).

    Demo mode uses the ``jobpipeline-demo`` keychain service and an empty environment, so it
    never picks up real credentials; the allow flag still comes from ``environ``. Environment
    secrets are registered with ``redactor`` here; the keychain is not touched.
    """
    environment = EnvSecretStore({} if demo else environ)
    register_environment_secrets(environment, redactor)
    keychain = KeyringSecretStore(
        DEMO_SERVICE_NAME if demo else SERVICE_NAME, backend=keyring_backend
    )
    secrets = ChainedSecretStore(keychain, environment, redactor=redactor)
    allow_fakes = environ.get(ALLOW_FAKES_VARIABLE) == "1"
    if allow_fakes:
        _log.warning("%s is set: fake AI providers are offered", ALLOW_FAKES_VARIABLE)
    return Wiring(
        redactor=redactor,
        secrets=secrets,
        environment=environment,
        allow_fakes=allow_fakes,
        demo=demo,
    )


def clear_ai_temp(data_dir: Path) -> Path:
    """Create ``<data>/tmp/ai`` (0700) and remove leftover call folders from it (ADR-057).

    ``<data>/tmp`` and ``<data>/tmp/ai`` must be real folders, not links: anything else
    raises ``OSError``. Only real folders named like a call folder are removed; links, files
    and other folders stay. A failed removal is logged by error type only.
    """
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = _real_folder(data_dir / AI_TEMP_FOLDER[0])
    folder = _real_folder(tmp / AI_TEMP_FOLDER[1])
    if os.name == "posix":
        folder.chmod(0o700)
    for entry in folder.iterdir():
        if not entry.name.startswith(CALL_FOLDER_PREFIX) or entry.is_symlink():
            continue
        if not entry.is_dir():
            continue
        try:
            shutil.rmtree(entry)
        except OSError as error:
            _log.warning("Couldn't remove a leftover AI call folder (%s)", type(error).__name__)
    return folder


def _real_folder(path: Path) -> Path:
    """Create ``path`` (0700) if missing and check it is a real folder, not a link."""
    path.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise NotADirectoryError("the AI temporary folder is a link or not a folder")
    return path


def _provider_name(kind: ProviderKind | None) -> str:
    """The provider a kind stands for (Claude when nothing is chosen yet)."""
    return FakeAIProvider.name if kind == "fake" else ClaudeSdkProvider.name


def _cli_present() -> bool:
    """A CLI file is there: the bundled one, or (Windows) a native ``claude.exe`` where the
    provider looks for it. Nothing is run or probed (ADR-058)."""
    try:
        bundled_cli_path()
        return True
    except ProviderNotInstalled:
        pass
    if sys.platform != "win32":
        return False
    return any(_is_file(path) for path in windows_cli_candidates(os.environ, cwd=Path.cwd()))


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False
