"""Claude provider built on the Claude Agent SDK (spec 2.3 rule 3, 2.9.7-10, 3.3; ADR-044).

The only module allowed to import ``claude_agent_sdk``. M1.1 adds the options builder,
M1.13 the provider's ``run`` with isolation, usage and error mapping, M1.14
``test_connection``, the model list, the private config folder and CLI resolution (the
bundled CLI, or a native ``claude.exe`` at the minimum version on Windows). Spike findings:
ADR-045; calls: ADR-057.

``ClaudeAgentOptions`` repr includes ``env`` and so the credential: never log options, or
errors that carry them. SDK errors embed stderr, result prose and paths, so mapped errors
keep only the error type and the HTTP status (spec 2.11). The SDK's own log records can
quote CLI output; ``app.logging_config.SdkRecordFilter`` reduces them on every handler.

Calls go through the public ``ClaudeSDKClient`` rather than ``query()``: ``query()`` nests
async generators, so closing it early (cancellation) leaves the CLI running until a garbage
collector finaliser runs. ``ClaudeSDKClient.disconnect()`` closes the transport in the
calling task, before the working folder is removed.
"""

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Final

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    Message,
    ProcessError,
    ResultError,
    ResultMessage,
)

# Not re-exported by the SDK's top level (0.2.163).
from claude_agent_sdk._errors import MessageParseError

from app.domain.errors import (
    AppError,
    InvalidModelOutput,
    NetworkUnavailable,
    ProviderAuthError,
    ProviderNotInstalled,
    ProviderRequestRejected,
    UsageLimitReached,
)
from app.domain.settings import ClaudeAuthMode
from app.modules.ai import (
    AICapabilities,
    AIRequest,
    AIResponse,
    CallCancelled,
    CancelToken,
    ConnectionResult,
    ModelChoice,
    TokenUsage,
)
from app.modules.secrets import ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, SecretStore

_LOG = logging.getLogger(__name__)

AuthMode = ClaudeAuthMode

# Structured output is delivered through a tool call inside the CLI, so one model turn
# may not be enough; the exact bound is confirmed by the live check (M1.25, ADR-045).
MAX_TURNS: Final = 2
WEB_SEARCH_TOOL: Final = "WebSearch"

# Tier defaults as aliases, so they follow the current models (ADR-044 item 9).
CLAUDE_FAST_DEFAULT: Final = "haiku"
CLAUDE_THOROUGH_DEFAULT: Final = "sonnet"
CLAUDE_MOST_CAPABLE: Final = "opus"
_KNOWN_MODELS: Final = (
    ModelChoice(CLAUDE_FAST_DEFAULT, "Claude Haiku, fast"),
    ModelChoice(CLAUDE_THOROUGH_DEFAULT, "Claude Sonnet, thorough"),
    ModelChoice(CLAUDE_MOST_CAPABLE, "Claude Opus, most capable"),
)

# The first CLI that honours ``verbatim_prompts`` (ADR-045); older ones ignore it.
MIN_CLI_VERSION: Final = "2.1.248"
CONNECTION_TIMEOUT_SECONDS: Final = 60.0
PROBE_TIMEOUT_SECONDS: Final = 10.0

# Which saved secret each sign-in mode reads, at call time.
SECRET_KEY_BY_MODE: Final[Mapping[AuthMode, str | None]] = {
    "subscription_token": CLAUDE_CODE_OAUTH_TOKEN,
    "api_key": ANTHROPIC_API_KEY,
    "existing_login": None,
}

_SUBSCRIPTION_VARIABLE: Final = "CLAUDE_CODE_OAUTH_TOKEN"
_API_KEY_VARIABLE: Final = "ANTHROPIC_API_KEY"
_BEARER_VARIABLE: Final = "ANTHROPIC_AUTH_TOKEN"
_CREDENTIAL_VARIABLE_BY_MODE: Final[Mapping[AuthMode, str | None]] = {
    "subscription_token": _SUBSCRIPTION_VARIABLE,
    "api_key": _API_KEY_VARIABLE,
    "existing_login": None,
}

# Inherited variables that could send prompts elsewhere (gateways, Bedrock, Vertex,
# OpenTelemetry exporters), switch models, print debug output or inject code into the CLI
# runtime, or pick another sign-in (CLAUDE_* covers the config and keychain folders,
# device and session tokens, and CLAUDE_LOCAL_OAUTH_*). The SDK passes the whole parent
# environment through, so each one found is blanked. Proxy settings and NODE_EXTRA_CA_CERTS
# are kept: they only see encrypted traffic and legitimate networks need them.
_BLANKED_PREFIXES: Final = ("ANTHROPIC_", "CLAUDE_", "OTEL_", "BUN_")
_BLANKED_NAMES: Final = frozenset(
    {
        "DEBUG",
        "USE_STAGING_OAUTH",
        "USE_LOCAL_OAUTH",
        "NODE_OPTIONS",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    }
)
# Never blanked: the SDK sets the first two itself (it adds the session-state flag only when
# the key is absent), and the CLI needs Git Bash's location on Windows.
_KEPT_NAMES: Final = frozenset(
    {"CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SDK_READS_SESSION_STATE", "CLAUDE_CODE_GIT_BASH_PATH"}
)
# Where the CLI keeps its sign-in: its config folder and the folder its keychain item name
# is derived from (an empty value selects the user's own login). Token and API-key modes
# point both at a private empty folder; existing_login keeps the inherited values.
_CONFIG_DIR_VARIABLE: Final = "CLAUDE_CONFIG_DIR"
_SECURE_STORAGE_VARIABLE: Final = "CLAUDE_SECURESTORAGE_CONFIG_DIR"
_SIGN_IN_FOLDER_VARIABLES: Final = frozenset({_CONFIG_DIR_VARIABLE, _SECURE_STORAGE_VARIABLE})
# No telemetry, error reporting or update traffic (spec 2.9.10).
_QUIET_SETTINGS: Final[Mapping[str, str]] = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
}


def build_options(
    *,
    mode: AuthMode,
    model: str,
    system: str,
    output_schema: Mapping[str, Any],
    workdir: Path,
    credential: str | None,
    allow_web_search: bool = False,
    environ: Mapping[str, str] | None = None,
    config_dir: Path | None = None,
) -> ClaudeAgentOptions:
    """Options for one isolated, structured-output call (spec 2.9.8).

    ``workdir`` must be an absolute path to an empty folder the caller owns. ``environ``
    is the environment the CLI would inherit (the process environment by default).
    ``config_dir`` (token and API-key modes only, required there) is an absolute, empty,
    private folder used as the CLI's config and keychain folder.
    """
    if not workdir.is_absolute():
        raise ValueError("The working folder must be an absolute path.")
    if (_CREDENTIAL_VARIABLE_BY_MODE[mode] is None) != (config_dir is None):
        raise ValueError("Token and API-key modes need a private config folder; no other.")
    if config_dir is not None and not config_dir.is_absolute():
        raise ValueError("The config folder must be an absolute path.")
    tools = [WEB_SEARCH_TOOL] if allow_web_search else []
    return ClaudeAgentOptions(
        tools=list(tools),
        allowed_tools=list(tools),
        permission_mode="dontAsk",
        system_prompt=system,
        model=model,
        max_turns=MAX_TURNS,
        output_format={"type": "json_schema", "schema": dict(output_schema)},
        cwd=workdir,
        # Nothing from the filesystem: no settings, CLAUDE.md, MCP servers, plugins or skills.
        setting_sources=[],
        strict_mcp_config=True,
        mcp_servers={},
        plugins=[],
        skills=[],
        # Untrusted text may contain "@/path"; deliver prompts as written (spec 3.3).
        verbatim_prompts=True,
        # Prompts carry personal data; don't keep transcripts under ~/.claude.
        extra_args={"no-session-persistence": None},
        env=_child_environment(
            mode, credential, os.environ if environ is None else environ, config_dir
        ),
    )


def _child_environment(
    mode: AuthMode,
    credential: str | None,
    inherited: Mapping[str, str],
    config_dir: Path | None,
) -> dict[str, str]:
    """Variables merged over the inherited environment for the CLI subprocess.

    Every credential variable the chosen mode doesn't use is blanked, so a key in the
    user's shell never bills instead of their subscription, and every inherited variable
    that could redirect or expose the call is blanked too.
    """
    target = _CREDENTIAL_VARIABLE_BY_MODE[mode]
    if target is None and credential is not None:
        raise ValueError("This sign-in mode takes no credential.")
    if target is not None and not credential:
        raise ValueError("This sign-in mode needs a saved credential.")
    environment = {name: "" for name in inherited if _is_blanked(name, mode)}
    environment.update({name: "" for name in _BLANKED_NAMES})
    environment.update({_SUBSCRIPTION_VARIABLE: "", _API_KEY_VARIABLE: "", _BEARER_VARIABLE: ""})
    environment.update(_QUIET_SETTINGS)
    if target is not None and credential is not None:
        environment[target] = credential
    if config_dir is not None:
        for name in _SIGN_IN_FOLDER_VARIABLES:
            environment[name] = str(config_dir)
    return environment


def _is_blanked(name: str, mode: AuthMode) -> bool:
    if name in _KEPT_NAMES:
        return False
    if mode == "existing_login" and name in _SIGN_IN_FOLDER_VARIABLES:
        return False  # the user's own sign-in lives there
    return name in _BLANKED_NAMES or name.startswith(_BLANKED_PREFIXES)


# --- the provider (M1.13) ----------------------------------------------------------------

_TEMP_PREFIX: Final = "jobpipeline-ai-"
_WORKDIR_NAME: Final = "work"  # the CLI's empty working folder, inside the call folder
# Token and API-key modes: the CLI's own config folder, private and empty per call, so no
# sign-in, settings or history from the user's ~/.claude is read or written (ADR-057).
_CONFIG_DIR_NAME: Final = "config"
_SYSTEM_PROMPT_NAME: Final = "system-prompt.md"
_SYNTHETIC_MODEL: Final = "<synthetic>"  # the CLI's model name on locally made error turns
_AUTH_STATUSES: Final = frozenset({401, 403})
_LIMIT_STATUSES: Final = frozenset({402, 429})
_AUTH_ASSISTANT_ERRORS: Final = frozenset({"authentication_failed"})
_LIMIT_ASSISTANT_ERRORS: Final = frozenset({"billing_error", "rate_limit"})
_REJECTED_ASSISTANT_ERRORS: Final = frozenset({"invalid_request"})
# Runs that ended without an answer of the right shape (spec 3.6: the executor may retry).
_OUTPUT_SUBTYPES: Final = frozenset({"error_max_turns", "error_max_structured_output_retries"})

ClientFactory = Callable[[ClaudeAgentOptions], ClaudeSDKClient]


def bundled_cli_path() -> Path:
    """The CLI bundled with the SDK, looked up on every call (ADR-045 follow-up).

    Never falls back to a ``claude`` on ``PATH``: an older CLI there would ignore
    ``verbatim_prompts``. Missing raises ``ProviderNotInstalled``.
    """
    path = _bundled_cli(sys.platform)
    if path is None:
        raise ProviderNotInstalled(detail="bundled CLI missing")
    return path


def _bundled_cli(platform: str) -> Path | None:
    name = "claude.exe" if platform == "win32" else "claude"
    path = Path(os.path.dirname(claude_agent_sdk.__file__)) / "_bundled" / name
    return path if path.is_file() else None


_WINDOWS_CLI_NAME: Final = "claude.exe"
# Where Claude Code's native Windows installer puts the CLI, under the user's profile.
_WINDOWS_INSTALLER_FOLDER: Final = (".local", "bin")


def windows_cli_candidates(environ: Mapping[str, str], *, cwd: Path) -> list[Path]:
    """Where a native ``claude.exe`` may be, in order: the installer location under
    ``USERPROFILE``, then each absolute ``PATH`` entry.

    Never the current folder: ``shutil.which`` on Windows searches it first, so a planted
    ``claude.exe`` there would be run. Empty, relative and current-folder entries are
    skipped.
    """
    candidates: list[Path] = []
    profile = environ.get("USERPROFILE", "")
    if profile and Path(profile).is_absolute():
        candidates.append(Path(profile).joinpath(*_WINDOWS_INSTALLER_FOLDER, _WINDOWS_CLI_NAME))
    for raw in environ.get("PATH", "").split(os.pathsep):
        entry = raw.strip().strip('"')
        if not entry or not Path(entry).is_absolute() or _same_folder(Path(entry), cwd):
            continue
        candidates.append(Path(entry) / _WINDOWS_CLI_NAME)
    return candidates


def _same_folder(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve()
    except (OSError, RuntimeError):
        return True  # can't tell: treat as unsafe


def resolve_cli_path(
    *,
    platform: str,
    candidates: Callable[[], Iterable[Path]],
    version_of: Callable[[Path], str | None],
) -> Path:
    """The CLI to run: the bundled one, else (Windows only) a native ``claude.exe`` whose
    version is at least ``MIN_CLI_VERSION`` (ADR-045 Windows update).

    macOS and Linux never look further than the bundled CLI. On Windows the first
    ``candidates`` entry that is an absolute, existing ``.exe`` file outside the current
    folder is resolved and only then probed; scripts (``.cmd``, ``.bat``, ``.ps1``),
    relative paths and anything in the current folder are never run. Anything else raises
    ``ProviderNotInstalled``, whose detail never holds a path or the CLI's output.
    """
    bundled = _bundled_cli(platform)
    if bundled is not None:
        return bundled
    if platform != "win32":
        raise ProviderNotInstalled(detail="bundled CLI missing")
    cwd = Path.cwd()
    for candidate in candidates():
        path = _safe_executable(candidate, cwd)
        if path is None:
            continue
        if not _version_at_least(version_of(path), MIN_CLI_VERSION):
            raise ProviderNotInstalled(detail="claude.exe older than the minimum or unreadable")
        return path
    raise ProviderNotInstalled(detail="no native claude.exe found")


def _safe_executable(candidate: Path, cwd: Path) -> Path | None:
    """``candidate`` resolved, if it is an absolute existing ``.exe`` file outside the
    current folder; otherwise ``None``."""
    if not candidate.is_absolute() or candidate.suffix.lower() != ".exe":
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_file() or resolved.suffix.lower() != ".exe":
        return None
    if _same_folder(candidate.parent, cwd) or _same_folder(resolved.parent, cwd):
        return None
    return resolved


class CliResolver:
    """The provider's default CLI lookup, run at call time on the real platform.

    A passing version probe is remembered per installed binary (path, modification time
    and size), so a native ``claude.exe`` is run once to check it, not on every call. The
    cache is per instance; a failed probe is not remembered.
    """

    def __init__(
        self,
        *,
        platform: str = sys.platform,
        candidates: Callable[[], Iterable[Path]] | None = None,
        version_of: Callable[[Path], str | None] | None = None,
    ) -> None:
        self._platform = platform
        self._candidates = candidates or (
            lambda: windows_cli_candidates(os.environ, cwd=Path.cwd())
        )
        self._version_of = version_of or probe_cli_version
        self._versions: dict[tuple[str, int, int], str] = {}

    def __call__(self) -> Path:
        return resolve_cli_path(
            platform=self._platform, candidates=self._candidates, version_of=self._version
        )

    def _version(self, path: Path) -> str | None:
        try:
            status = path.stat()
        except OSError:
            return None
        key = (str(path), status.st_mtime_ns, status.st_size)
        known = self._versions.get(key)
        if known is not None:
            return known
        reported = self._version_of(path)
        if reported is not None:
            self._versions = {key: reported}  # only the current binary is kept
        return reported


_VERSION_PATTERN: Final = re.compile(r"\s*(\d+)\.(\d+)\.(\d+)\b")


def _version_at_least(reported: str | None, minimum: str) -> bool:
    if reported is None:
        return False
    match = _VERSION_PATTERN.match(reported)
    if match is None:
        return False
    found = tuple(int(part) for part in match.groups())
    wanted = tuple(int(part) for part in minimum.split("."))
    return found >= wanted


# Only what a CLI needs to start; never credentials, routing or runtime-injection variables.
_PROBE_VARIABLES: Final = ("PATH", "HOME", "SYSTEMROOT", "USERPROFILE", "TEMP", "TMP")
PROBE_READ_LIMIT: Final = 4096  # bytes of stdout read from the probe, at most
_PROBE_LINE_LIMIT: Final = 200
_PROBE_KILL_WAIT_SECONDS: Final = 2.0


def probe_cli_version(path: Path, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> str | None:
    """The first line ``<path> --version`` prints, or ``None`` if it fails or times out.

    Runs with a minimal environment (no ``ANTHROPIC_*``, ``CLAUDE_*``, ``NODE_*`` or
    ``JOBPIPELINE_*`` values), no stdin and stderr discarded. At most ``PROBE_READ_LIMIT``
    bytes are read; a child still running at the timeout (or after filling that) is killed.
    Output is never logged. Blocking: call it off the event loop.
    """
    environment = {name: os.environ[name] for name in _PROBE_VARIABLES if name in os.environ}
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed arguments, no shell
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    chunks: list[bytes] = []
    reader = threading.Thread(target=_read_into, args=(process, chunks), daemon=True)
    try:
        output = _read_bounded(process, reader, chunks, timeout)
    finally:
        _stop(process, reader)
    if output is None or process.returncode != 0:
        return None
    lines = output.decode("utf-8", errors="replace").strip().splitlines()
    return lines[0][:_PROBE_LINE_LIMIT] if lines else None


def _read_into(process: "subprocess.Popen[bytes]", chunks: list[bytes]) -> None:
    if process.stdout is not None:
        with contextlib.suppress(OSError, ValueError):
            chunks.append(process.stdout.read(PROBE_READ_LIMIT))


def _read_bounded(
    process: "subprocess.Popen[bytes]",
    reader: threading.Thread,
    chunks: list[bytes],
    timeout: float,
) -> bytes | None:
    """Up to ``PROBE_READ_LIMIT`` bytes of stdout, if the child exits within ``timeout``."""
    deadline = time.monotonic() + timeout
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        return None
    try:
        process.wait(max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return None  # still running (or blocked writing more than the limit)
    return chunks[0] if chunks else None


def _stop(process: "subprocess.Popen[bytes]", reader: threading.Thread) -> None:
    """Kill the child if it still runs and wait briefly. The pipe is closed only once the
    reader has finished: closing a buffered pipe another thread is reading can block."""
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(_PROBE_KILL_WAIT_SECONDS)
    if reader.ident is not None:  # started
        reader.join(_PROBE_KILL_WAIT_SECONDS)
    if process.stdout is not None and not reader.is_alive():
        with contextlib.suppress(OSError):
            process.stdout.close()


def _discard_stderr(_line: str) -> None:
    """CLI stderr can quote prompts or credentials: it is dropped, never stored, printed or
    logged. Without a callback the SDK would pass it to the terminal."""


class ClaudeSdkProvider:
    """Runs one isolated, structured-output call per request (spec 2.9.7-10, 3.3).

    The credential is read from ``secrets`` at call time, never kept. Each call gets a
    private folder under ``temp_root`` (the system temp folder by default) holding the
    CLI's empty working folder, the system prompt file (0600, so the prompt isn't on
    the command line) and, in token and API-key modes, the CLI's private empty config
    folder (``CLAUDE_CONFIG_DIR``; ``existing_login`` keeps the inherited one, where the
    user's sign-in lives); the folder is removed after the call. ``client_factory`` builds
    the SDK client (tests inject one with a replaying transport); ``cli_path`` finds the CLI
    at call time (a per-instance ``CliResolver`` by default). ``test_connection`` gives up
    after ``connection_timeout`` seconds, CLI lookup included.
    """

    name: Final = "claude"
    capabilities: Final = AICapabilities(web_search=True, reports_token_usage=True)

    def __init__(
        self,
        *,
        secrets: SecretStore,
        mode: AuthMode,
        client_factory: ClientFactory = ClaudeSDKClient,
        cli_path: Callable[[], Path] | None = None,
        environ: Mapping[str, str] | None = None,
        temp_root: Path | None = None,
        connection_timeout: float = CONNECTION_TIMEOUT_SECONDS,
    ) -> None:
        self._secrets = secrets
        self._mode: AuthMode = mode
        self._client_factory = client_factory
        # Per instance, so a native claude.exe's version is probed once per binary.
        self._cli_path = cli_path if cli_path is not None else CliResolver()
        self._environ = environ
        self._temp_root = temp_root
        self._connection_timeout = connection_timeout

    async def test_connection(self, model: str) -> ConnectionResult:
        """One tiny single-turn, no-tool request with fixed texts (AC-M1.3).

        Goes through the same path as ``run``. Any JSON object answer means connected; a
        mapped failure, or no answer within ``connection_timeout``, becomes its message.
        """
        request = dataclasses.replace(_CONNECTION_REQUEST, model=model)
        try:
            response = await self._call(request, _NOT_CANCELLED, self._connection_timeout)
        except _CONNECTION_FAILURES as error:
            return ConnectionResult(ok=False, message=error.user_message, model=None)
        return ConnectionResult(ok=True, message="Connected", model=response.model)

    def known_models(self) -> list[ModelChoice]:
        return list(_KNOWN_MODELS)

    async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
        return await self._call(request, cancel, None)

    async def _call(
        self, request: AIRequest, cancel: CancelToken, timeout: float | None
    ) -> AIResponse:
        if cancel.cancelled:
            raise CallCancelled
        credential = self._credential()
        # asyncio.timeout ties the provider to asyncio, which the app runs on (uvicorn).
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        try:
            async with asyncio.timeout_at(deadline):
                # Off the event loop: finding the CLI may run its version probe.
                cli = await asyncio.to_thread(self._cli_path)
        except TimeoutError:
            raise NetworkUnavailable(detail="timeout") from None
        folder = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX, dir=self._temp_root)).resolve()
        try:
            return await self._run_in(folder, request, credential, cli, cancel, deadline)
        finally:
            _remove_folder(folder)

    def _credential(self) -> str | None:
        key = SECRET_KEY_BY_MODE[self._mode]
        if key is None:
            return None
        value = self._secrets.get(key)
        if value is None or not value.strip():
            raise ProviderAuthError(detail="credential missing")
        return value

    async def _run_in(
        self,
        folder: Path,
        request: AIRequest,
        credential: str | None,
        cli: Path,
        cancel: CancelToken,
        deadline: float | None,
    ) -> AIResponse:
        workdir = folder / _WORKDIR_NAME
        workdir.mkdir(mode=0o700)
        system_file = _write_private(folder / _SYSTEM_PROMPT_NAME, request.system)
        config_dir: Path | None = None
        if SECRET_KEY_BY_MODE[self._mode] is not None:  # token and API-key modes
            config_dir = folder / _CONFIG_DIR_NAME
            config_dir.mkdir(mode=0o700)
        options = dataclasses.replace(
            build_options(
                mode=self._mode,
                model=request.model,
                system=request.system,
                output_schema=request.output_schema,
                workdir=workdir,
                credential=credential,
                allow_web_search=request.allow_web_search,
                environ=self._environ,
                config_dir=config_dir,
            ),
            system_prompt={"type": "file", "path": str(system_file)},
            cli_path=cli,
            stderr=_discard_stderr,
        )
        run = _RunState()
        mapped: AppError | None = None
        cancelled = False
        client = self._client_factory(options)
        try:
            # The timeout ends before the client is closed, so closing is never cancelled.
            async with asyncio.timeout_at(deadline):
                await client.connect(request.user)
                async for message in client.receive_response():
                    if cancel.cancelled:
                        cancelled = True
                        break
                    run.observe(message)
        except TimeoutError:
            mapped = NetworkUnavailable(detail="timeout")
        except ClaudeSDKError as error:
            mapped = _map_sdk_error(error, run)
        except Exception as error:  # control-protocol timeouts, raw transport errors
            mapped = NetworkUnavailable(detail=type(error).__name__)
        finally:
            await _disconnect(client)
        # Raised outside the except blocks, so no SDK error (with its stderr, prose or
        # paths) is chained to the mapped one.
        if cancelled:
            raise CallCancelled
        if mapped is not None:
            raise mapped
        return run.response(request.model)


# --- test_connection (M1.14) --------------------------------------------------------------

# Fixed tiny texts: no personal data, nothing from the store, the same for every provider.
_CONNECTION_REQUEST: Final = AIRequest(
    task="test_connection",
    prompt_id="test_connection",
    prompt_version="1",
    system='This is a connection check. Reply only with the JSON object {"ok": true}.',
    user="Connection check.",
    output_schema={
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    },
    model=CLAUDE_FAST_DEFAULT,
)
# Mapped failures become the result's message; anything else (cancellation) propagates.
_CONNECTION_FAILURES: Final = (
    ProviderAuthError,
    ProviderNotInstalled,
    UsageLimitReached,
    NetworkUnavailable,
    ProviderRequestRejected,
    InvalidModelOutput,
)


class _NeverCancelled:
    @property
    def cancelled(self) -> bool:
        return False


_NOT_CANCELLED: Final = _NeverCancelled()


def _write_private(path: Path, text: str) -> Path:
    """Write ``text`` to a new file only the owner can read (0600 on POSIX)."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _remove_folder(folder: Path) -> None:
    """Remove the call folder; a failure is logged by error type only (never the path) and
    never replaces the call's result or error."""
    try:
        shutil.rmtree(folder)
    except OSError as error:
        _LOG.warning("Could not remove a temporary AI working folder (%s)", type(error).__name__)


async def _disconnect(client: ClaudeSDKClient) -> None:
    """Close the SDK client in this task: stops the CLI subprocess before the working
    folder is removed. A failure while closing has nothing more to report."""
    with contextlib.suppress(Exception):
        await client.disconnect()


class _RunState:
    """What one call's messages reported."""

    def __init__(self) -> None:
        self.model: str | None = None
        self.assistant_error: str | None = None
        self.result: ResultMessage | None = None

    def observe(self, message: Message) -> None:
        if isinstance(message, AssistantMessage):
            if message.model and message.model != _SYNTHETIC_MODEL:
                self.model = message.model
            if message.error is not None:
                self.assistant_error = message.error
        elif isinstance(message, ResultMessage):
            self.result = message

    def response(self, requested_model: str) -> AIResponse:
        result = self.result
        if result is None:
            raise InvalidModelOutput(detail="no result message")
        # The result decides failure; an assistant error only helps to classify it.
        if result.is_error:
            raise _classify(
                "ResultMessage", result.api_error_status, self.assistant_error, result.subtype
            )
        output = result.structured_output
        if not isinstance(output, dict):
            raise InvalidModelOutput(detail="structured output is not a JSON object")
        return AIResponse(
            raw_text=json.dumps(output, ensure_ascii=False),
            data=output,
            model=self._reported_model(result) or requested_model,
            usage=_usage(result.usage),
        )

    def _reported_model(self, result: ResultMessage) -> str | None:
        if self.model:
            return self.model
        if isinstance(result.model_usage, dict) and len(result.model_usage) == 1:
            (only,) = result.model_usage
            if isinstance(only, str) and only.strip():
                return only
        return None


def _usage(reported: Mapping[str, Any] | None) -> TokenUsage | None:
    if not reported:
        return None
    input_tokens = reported.get("input_tokens")
    output_tokens = reported.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)


def _map_sdk_error(error: ClaudeSDKError, run: _RunState) -> AppError:
    """The spec 2.11 error for an SDK exception. Detail: the type name, plus the HTTP
    status for a ``ResultError``; never the message, stderr or result text."""
    if isinstance(error, CLINotFoundError):
        return ProviderNotInstalled(detail="CLINotFoundError")
    if isinstance(error, ResultError):
        return _classify("ResultError", error.api_error_status, run.assistant_error, error.subtype)
    if isinstance(error, CLIJSONDecodeError | MessageParseError):
        return InvalidModelOutput(detail=type(error).__name__)
    if isinstance(error, CLIConnectionError | ProcessError):
        return NetworkUnavailable(detail=type(error).__name__)
    return NetworkUnavailable(detail="ClaudeSDKError")


def _classify(
    source: str, status: int | None, assistant_error: str | None, subtype: str | None
) -> AppError:
    detail = source if status is None else f"{source} status={status}"
    if status in _AUTH_STATUSES or assistant_error in _AUTH_ASSISTANT_ERRORS:
        return ProviderAuthError(detail=detail)
    if status in _LIMIT_STATUSES or assistant_error in _LIMIT_ASSISTANT_ERRORS:
        return UsageLimitReached(detail=detail)
    if assistant_error in _REJECTED_ASSISTANT_ERRORS or (
        status is not None and 400 <= status < 500
    ):
        return ProviderRequestRejected(detail=detail)
    if subtype in _OUTPUT_SUBTYPES:
        return InvalidModelOutput(detail=detail)
    # Server errors (5xx, 529), connection failures and anything unrecognised.
    return NetworkUnavailable(detail=detail)
