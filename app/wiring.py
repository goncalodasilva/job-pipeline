"""The composition root for providers (spec 2.3 rule 7, 2.9.9, 2.9.12; ADR-030).

``build_wiring`` builds the one secret store the app uses (keychain first, then the
environment, every value registered with the log redactor; ADR-047), decides once whether
fake providers may be chosen (``JOBPIPELINE_ALLOW_FAKE_PROVIDERS`` exactly ``"1"``, or a demo
run on the demo's own folder; ADR-044 item 2, ADR-054, ADR-077's M2.20 checkpoint update) and
returns a ``Wiring``, the app's ``AIProviderFactory``. ``demo`` and ``demo_workspace`` come
from the start-up configuration alone (the ``--demo`` flag, and the data folder being the one
the app chose for the demo: ``app.config.is_demo_workspace``): no request, setting or file in
the data folder can turn them on.

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
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from types import MappingProxyType
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from app.domain.clock import Clock, SystemClock
from app.domain.errors import ProviderNotInstalled
from app.domain.redaction import Redactor
from app.domain.settings import AIProviderSettings, ProviderKind, Tier, claude_auth_mode
from app.domain.tiers import resolve_model
from app.modules.ai import (
    AIProvider,
    AITaskRunner,
    ModelChoice,
    PromptEditor,
    ProviderChoice,
    ProviderHealth,
)
from app.modules.ai.executor import TaskExecutor
from app.modules.ai.prompts import PromptLibrary
from app.modules.ai.providers.claude_sdk import (
    CALL_FOLDER_PREFIX,
    CLAUDE_FAST_DEFAULT,
    CLAUDE_THOROUGH_DEFAULT,
    DISCONNECT_TIMEOUT_SECONDS,
    SECRET_KEY_BY_MODE,
    ClaudeSdkProvider,
    bundled_cli_path,
    windows_cli_candidates,
)
from app.modules.ai.providers.claude_sdk import KNOWN_MODELS as CLAUDE_MODELS
from app.modules.ai.providers.fake import (
    FAKE_FAST_MODEL,
    FAKE_THOROUGH_MODEL,
    PRACTICE_TASK,
    FakeAIProvider,
    FakeOutcome,
    outcome_for_key,
    practice_responses,
)
from app.modules.ai.providers.fake import KNOWN_MODELS as FAKE_MODELS
from app.modules.ai.providers.fake_scripts import real_task_scripts
from app.modules.ai.providers.job_scripts import (
    angles_scripts,
    cv_template_scripts,
    draft_cover_letter_scripts,
    fit_scripts,
    gap_check_scripts,
    summary_scripts,
    tailor_cv_scripts,
)
from app.modules.ai.providers.session_scripts import (
    enrichment_question_scripts,
    structure_answer_scripts,
)
from app.modules.ai.tasks import PRACTICE_SPEC, TASK_SPECS, TaskSpec
from app.modules.calendar import CalendarProvider
from app.modules.calendar.providers.ics import IcsCalendarProvider
from app.modules.fetch import JobPageFetcher
from app.modules.fetch.providers.demo_pages import demo_pages
from app.modules.fetch.providers.fake import FakeHostRouter, FakeJobPageFetcher
from app.modules.fetch.providers.guarded import GuardedJobPageFetcher
from app.modules.search import SearchProvider, TransientSearchResults
from app.modules.search.providers.delegated import DelegatedAISearch
from app.modules.search.providers.fake import FakeSearchProvider
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
from app.render import DocumentRenderer
from app.render.providers.pdf import PdfRenderer
from app.render.providers.word import WordRenderer

__all__ = [
    "AI_TEMP_FOLDER",
    "PROVIDER_RUN_TIMEOUT",
    "TASK_TIMEOUT_FLOOR",
    "Wiring",
    "build_wiring",
    "clear_ai_temp",
    "local_timezone",
]

ALLOW_FAKES_VARIABLE: Final = "JOBPIPELINE_ALLOW_FAKE_PROVIDERS"
AI_TEMP_FOLDER: Final = ("tmp", "ai")  # under the data folder (ADR-057)
PROMPTS_DIR: Final = Path(__file__).resolve().parent.parent / "prompts"
OVERRIDES_FOLDER: Final = ("prompts", "overrides")
# The overall limit on one provider run: M2.13 injects it into the Claude provider's
# ``run``. Every task type's watchdog timeout must be longer (spec 5 M2 reliability rule 5),
# so a slow but live run is never taken for a hung task; TASK_TIMEOUT_FLOOR adds the close.
PROVIDER_RUN_TIMEOUT: Final = timedelta(minutes=5)
# The shortest task-type timeout create_app accepts: a provider call may run for
# PROVIDER_RUN_TIMEOUT and then spend up to DISCONNECT_TIMEOUT_SECONDS closing the client (the
# CLI subprocess), plus a small margin, so the watchdog never interrupts a call still closing.
TASK_TIMEOUT_FLOOR: Final = (
    PROVIDER_RUN_TIMEOUT + timedelta(seconds=DISCONNECT_TIMEOUT_SECONDS) + timedelta(seconds=5)
)

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
        demo_workspace: bool = False,
        clock: Clock | None = None,
    ) -> None:
        self.redactor = redactor
        self.secrets = secrets  # the only store injected anywhere (ADR-047)
        self._environment = environment
        self.allow_fakes = allow_fakes
        self.demo = demo
        self.search_results = TransientSearchResults()
        # A demo run on the demo's own folder: the fake is offered by itself there (M2.26).
        self.demo_workspace = demo and demo_workspace
        # When providers observe usage (M2.2); the app's clock when ``create_app`` builds it.
        self.clock: Clock = clock if clock is not None else SystemClock()
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
            return _saved_fake()
        mode = claude_auth_mode(kind)
        if mode is None:  # pragma: no cover - every other kind is a Claude kind
            raise ValueError("This AI provider isn't available.")
        return ClaudeSdkProvider(
            secrets=self.secrets,
            mode=mode,
            temp_root=data_dir.joinpath(*AI_TEMP_FOLDER),
            clock=self.clock,
            run_timeout=PROVIDER_RUN_TIMEOUT,
        )

    def search_provider(self, provider: AIProvider) -> SearchProvider | None:
        """Select metadata only for an AI provider that can run metered searches."""
        if not provider.capabilities.web_search:
            return None
        if provider.name == "claude":
            return DelegatedAISearch()
        if provider.name == "fake" and self.allow_fakes:
            return FakeSearchProvider()
        return None

    def search_settings(self, settings: AIProviderSettings) -> SearchProvider | None:
        """Settings metadata without building a provider or reading credentials."""
        if settings.kind not in self._offered():
            return None
        if settings.kind == "fake" and self.allow_fakes:
            return FakeSearchProvider()
        if settings.kind and claude_auth_mode(settings.kind) is not None:
            return DelegatedAISearch() if ClaudeSdkProvider.capabilities.web_search else None
        return None

    def fake_for_key(self, key: str | None) -> FakeAIProvider:
        """The fake whose connection test follows the fictional keywords in ``key``. Only
        the outcome is kept, never the key (ADR-054). Without a keyword (nothing pasted
        included) the test is rejected like a bad key, except in a demo workspace, where
        there is nothing to paste and it connects (M2.26).

        Raises ``ValueError`` while fakes aren't allowed; routes check the chosen kind
        against ``provider_choices()`` first (M1.17).
        """
        if not self.allow_fakes:
            raise ValueError("Fake providers are not allowed.")
        otherwise: FakeOutcome = "connected" if self.demo_workspace else "auth"
        return FakeAIProvider(connection=outcome_for_key(key, otherwise=otherwise), app_mode=True)

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

    def default_models(self, kind: ProviderKind | None) -> Mapping[Tier, str]:
        """The default model per tier of the provider ``kind`` stands for; builds nothing."""
        return MappingProxyType(dict(_DEFAULTS_BY_PROVIDER[_provider_name(kind)]))

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
        ``<data>/prompts/overrides`` (nothing is created here). Over the fake, while fakes
        are allowed (the allow variable, or a demo workspace), it also knows the practice
        task (M2.17). Raises ``ValueError`` for a provider wiring doesn't know."""
        defaults = _DEFAULTS_BY_PROVIDER.get(provider.name)
        if defaults is None:
            raise ValueError("Unknown AI provider.")
        prompts = _prompt_library(data_dir)
        specs: Mapping[str, TaskSpec] = TASK_SPECS
        if self._is_allowed_fake(provider):
            specs = MappingProxyType({**TASK_SPECS, PRACTICE_SPEC.name: PRACTICE_SPEC})
        return TaskExecutor(
            provider=provider,
            prompts=prompts,
            settings=settings,
            provider_defaults=defaults,
            specs=specs,
        )

    def prompt_editor(self, data_dir: Path) -> PromptEditor:
        """The prompt editor (M9.3) over the defaults and the ``<data>/prompts/overrides``
        folder the task runner reads: what it saves is what the next task uses. Nothing is
        created here."""
        return _prompt_library(data_dir)

    def script_practice(self, provider: AIProvider, *, outcome: str, delay_seconds: float) -> None:
        """Script the fake for one practice run (M2.17): every call takes ``delay_seconds``
        and ``outcome`` says how the run goes. Raises ``ValueError`` for any provider but
        the fake, while fakes aren't allowed, or for an unknown outcome."""
        if not isinstance(provider, FakeAIProvider) or not self._is_allowed_fake(provider):
            raise ValueError("The practice task needs the fake provider.")
        provider.script(PRACTICE_TASK, *practice_responses(outcome, delay_seconds=delay_seconds))

    def job_page_fetcher(self) -> JobPageFetcher:
        """The fetcher for job pages: the SSRF-guarded one. While fakes are allowed (tests,
        the demo), ``*.example.test`` URLs are served from the app's fictional demo pages
        instead, and every other URL still goes through the guarded fetcher (ADR-098
        item 2)."""
        guarded = GuardedJobPageFetcher()
        if not self.allow_fakes:
            return guarded
        return FakeHostRouter(fake=FakeJobPageFetcher(demo_pages()), real=guarded)

    def _is_allowed_fake(self, provider: AIProvider) -> bool:
        return self.allow_fakes and provider.name == FakeAIProvider.name

    def calendar_provider(self) -> CalendarProvider:
        """The local file provider needs no credentials or settings."""
        return IcsCalendarProvider(self.clock)

    def document_renderers(self) -> tuple[DocumentRenderer, ...]:
        return (PdfRenderer(self.clock), WordRenderer(self.clock))

    def _offered(self) -> frozenset[ProviderKind]:
        return frozenset(choice.kind for choice in self.provider_choices())


def build_wiring(
    *,
    environ: Mapping[str, str],
    demo: bool,
    redactor: Redactor,
    keyring_backend: KeyringBackend | None = None,
    clock: Clock | None = None,
    demo_workspace: bool = False,
) -> Wiring:
    """Compose the secret store and decide whether fakes are offered (ADR-047, ADR-054).

    Demo mode uses the ``jobpipeline-demo`` keychain service and an empty environment, so it
    never picks up real credentials. ``demo_workspace`` (``is_demo_workspace(config)``: the
    demo on the folder the app chose for it) offers the fake provider by itself, so practice
    tasks work in the demo (M2.26). Everywhere else, a demo on a folder the user named
    included, the allow flag in ``environ`` must be exactly ``"1"``. Environment secrets are
    registered with ``redactor`` here; the keychain is not touched.
    """
    environment = EnvSecretStore({} if demo else environ)
    register_environment_secrets(environment, redactor)
    keychain = KeyringSecretStore(
        DEMO_SERVICE_NAME if demo else SERVICE_NAME, backend=keyring_backend
    )
    secrets = ChainedSecretStore(keychain, environment, redactor=redactor)
    by_variable = environ.get(ALLOW_FAKES_VARIABLE) == "1"
    in_demo = demo and demo_workspace
    if by_variable:
        _log.warning("%s is set: fake AI providers are offered", ALLOW_FAKES_VARIABLE)
    elif in_demo:
        _log.info("Demo run: the fake AI provider is offered")
    return Wiring(
        redactor=redactor,
        secrets=secrets,
        environment=environment,
        allow_fakes=in_demo or by_variable,
        demo=demo,
        demo_workspace=in_demo,
        clock=clock,
    )


class _LocalTimezone(tzinfo):
    """The computer's time zone with its daylight-saving rules, asked from the operating
    system for each instant (no zone name to look up, no extra dependency). It reads zone
    rules only, never the current time."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None if dt is None else _system_local(dt).utcoffset()

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None  # not known apart from the offset; nothing here needs it

    def tzname(self, dt: datetime | None) -> str | None:
        return None if dt is None else _system_local(dt).tzname()

    def fromutc(self, dt: datetime) -> datetime:
        offset = dt.replace(tzinfo=UTC).astimezone().utcoffset()
        if offset is None:  # pragma: no cover - an aware datetime always has an offset
            raise ValueError("The local time zone has no offset.")
        local = dt + offset
        # The second pass through a repeated hour (clocks set back) is the ``fold``.
        return local if self.utcoffset(local) == offset else local.replace(fold=1)

    def __repr__(self) -> str:
        return "local_timezone()"


def _system_local(wall: datetime) -> datetime:
    """A wall-clock time read as the system's local time (``fold`` picks a repeated hour)."""
    return wall.replace(tzinfo=None).astimezone()


def local_timezone() -> tzinfo:
    """The time zone budget cycles use: the computer's own (ADR-076 item 8). ``create_app``
    takes another one for tests."""
    return _LocalTimezone()


def system_time_zone_name() -> str | None:
    """Read only the system link's IANA name; copied files, unknown or unlisted zones ask the user.

    No shell, environment override, user configuration, or inferred fixed-offset name.
    macOS's /var/db/timezone/zoneinfo links and relative Linux links work alike.
    """
    try:
        parts = Path("/etc/localtime").readlink().parts
        marker = parts.index("zoneinfo")
        name = "/".join(parts[marker + 1 :])
        ZoneInfo(name)
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return None
    # Only a name the interview form's own check accepts: the default is never refused.
    return name if name in available_timezones() else None


def _prompt_library(data_dir: Path) -> PromptLibrary:
    return PromptLibrary(
        defaults_dir=PROMPTS_DIR, overrides_dir=data_dir.joinpath(*OVERRIDES_FOLDER)
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


def _saved_fake() -> FakeAIProvider:
    """The fake as the saved provider: it answers real tasks from the fictional keyword
    scripts (ADR-088; ADR-089 item 8). The connection-test fake (``fake_for_key``) never
    does."""
    provider = FakeAIProvider(app_mode=True, web_search=True)
    provider.script_keywords("summarize_job", summary_scripts())
    provider.script_keywords("assess_fit", fit_scripts())
    provider.script_keywords("gap_check", gap_check_scripts())
    provider.script_keywords("propose_angles", angles_scripts())
    provider.script_keywords("draft_cv_template", cv_template_scripts())
    provider.script_keywords("tailor_cv", tailor_cv_scripts())
    provider.script_keywords("draft_cover_letter", draft_cover_letter_scripts())
    provider.script_keywords("enrichment_question", enrichment_question_scripts())
    provider.script_keywords("structure_answer", structure_answer_scripts())
    for task, script in real_task_scripts().items():
        provider.script_keywords(task, script)
    return provider


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
