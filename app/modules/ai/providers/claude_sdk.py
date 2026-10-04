"""Claude provider built on the Claude Agent SDK (spec 2.3 rule 3, 2.9.7-10, 3.3; ADR-044).

The only module allowed to import ``claude_agent_sdk``. M1.1 adds the options builder;
the provider itself (run, error mapping, ``test_connection``) comes in M1.13 and M1.14.
Spike findings: ADR-045.

``ClaudeAgentOptions`` repr includes ``env`` and so the credential: never log options, or
errors that carry them.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

from claude_agent_sdk import ClaudeAgentOptions

AuthMode = Literal["subscription_token", "existing_login", "api_key"]

# Structured output is delivered through a tool call inside the CLI, so one model turn
# may not be enough; the exact bound is confirmed by the live check (M1.25, ADR-045).
MAX_TURNS: Final = 2
WEB_SEARCH_TOOL: Final = "WebSearch"

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
