"""Claude provider built on the Claude Agent SDK (spec 2.3 rule 3, 2.9.7-10, 3.3; ADR-044).

The only module allowed to import ``claude_agent_sdk``. M1.1 adds the options builder,
M1.13 the provider's ``run`` with isolation, usage and error mapping; ``test_connection``
and the model list come in M1.14. Spike findings: ADR-045.

``ClaudeAgentOptions`` repr includes ``env`` and so the credential: never log options, or
errors that carry them. SDK errors embed stderr, result prose and paths, so mapped errors
keep only the error type and the HTTP status (spec 2.11). The SDK's own log records can
quote CLI output; ``app.logging_config.SdkRecordFilter`` reduces them on every handler.

Calls go through the public ``ClaudeSDKClient`` rather than ``query()``: ``query()`` nests
async generators, so closing it early (cancellation) leaves the CLI running until a garbage
collector finaliser runs. ``ClaudeSDKClient.disconnect()`` closes the transport in the
calling task, before the working folder is removed.
"""

import contextlib
import dataclasses
import json
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
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
# runtime. The SDK passes the whole parent environment through, so each one found is
# blanked. Proxy settings and NODE_EXTRA_CA_CERTS are kept: they only see encrypted traffic
# and legitimate networks need them.
_BLANKED_PREFIXES: Final = ("ANTHROPIC_", "CLAUDE_CODE_", "OTEL_", "BUN_")
_BLANKED_NAMES: Final = frozenset(
    {
        "DEBUG",
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
) -> ClaudeAgentOptions:
    """Options for one isolated, structured-output call (spec 2.9.8).

    ``workdir`` must be an absolute path to an empty folder the caller owns. ``environ``
    is the environment the CLI would inherit (the process environment by default).
    """
    if not workdir.is_absolute():
        raise ValueError("The working folder must be an absolute path.")
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
        env=_child_environment(mode, credential, os.environ if environ is None else environ),
    )


def _child_environment(
    mode: AuthMode, credential: str | None, inherited: Mapping[str, str]
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
    environment = {name: "" for name in inherited if _is_blanked(name)}
    environment.update({name: "" for name in _BLANKED_NAMES})
    environment.update({_SUBSCRIPTION_VARIABLE: "", _API_KEY_VARIABLE: "", _BEARER_VARIABLE: ""})
    environment.update(_QUIET_SETTINGS)
    if target is not None and credential is not None:
        environment[target] = credential
    return environment


def _is_blanked(name: str) -> bool:
    if name in _KEPT_NAMES:
        return False
    return name in _BLANKED_NAMES or name.startswith(_BLANKED_PREFIXES)


# --- the provider (M1.13) ----------------------------------------------------------------

_TEMP_PREFIX: Final = "jobpipeline-ai-"
_WORKDIR_NAME: Final = "work"  # the CLI's empty working folder, inside the call folder
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
    name = "claude.exe" if sys.platform == "win32" else "claude"
    path = Path(os.path.dirname(claude_agent_sdk.__file__)) / "_bundled" / name
    if not path.is_file():
        raise ProviderNotInstalled(detail="bundled CLI missing")
    return path


def _discard_stderr(_line: str) -> None:
    """CLI stderr can quote prompts or credentials: it is dropped, never stored, printed or
    logged. Without a callback the SDK would pass it to the terminal."""


class ClaudeSdkProvider:
    """Runs one isolated, structured-output call per request (spec 2.9.7-10, 3.3).

    The credential is read from ``secrets`` at call time, never kept. Each call gets a
    private folder under ``temp_root`` (the system temp folder by default) holding the
    CLI's empty working folder and the system prompt file (0600, so the prompt isn't on
    the command line); the folder is removed after the call. ``client_factory`` builds the
    SDK client (tests inject one with a replaying transport).
    """

    name: Final = "claude"
    capabilities: Final = AICapabilities(web_search=True, reports_token_usage=True)

    def __init__(
        self,
        *,
        secrets: SecretStore,
        mode: AuthMode,
        client_factory: ClientFactory = ClaudeSDKClient,
        cli_path: Callable[[], Path] = bundled_cli_path,
        environ: Mapping[str, str] | None = None,
        temp_root: Path | None = None,
    ) -> None:
        self._secrets = secrets
        self._mode: AuthMode = mode
        self._client_factory = client_factory
        self._cli_path = cli_path
        self._environ = environ
        self._temp_root = temp_root

    async def test_connection(self, model: str) -> ConnectionResult:
        raise NotImplementedError("test_connection arrives in M1.14.")

    def known_models(self) -> list[ModelChoice]:
        return [
            ModelChoice(CLAUDE_FAST_DEFAULT, "Haiku"),
            ModelChoice(CLAUDE_THOROUGH_DEFAULT, "Sonnet"),
        ]

    async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse:
        if cancel.cancelled:
            raise CallCancelled
        credential = self._credential()
        cli = self._cli_path()
        folder = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX, dir=self._temp_root)).resolve()
        try:
            return await self._run_in(folder, request, credential, cli, cancel)
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
    ) -> AIResponse:
        workdir = folder / _WORKDIR_NAME
        workdir.mkdir(mode=0o700)
        system_file = _write_private(folder / _SYSTEM_PROMPT_NAME, request.system)
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
            await client.connect(request.user)
            async for message in client.receive_response():
                if cancel.cancelled:
                    cancelled = True
                    break
                run.observe(message)
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
