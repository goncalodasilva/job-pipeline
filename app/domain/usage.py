"""Budget services and usage-record names (spec 3.7; ADR-076 item 4, ADR-078).

Pure: no I/O. The schema keeps ``service`` as free text (ADR-078 update, M2.5); this module
owns the set.
"""

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, TypeGuard

ServiceId = Literal["claude_subscription", "claude_api_key", "web_search", "fake"]

# Display names, shown in budget dialogs and errors (spec 3.10: fixed English text).
SERVICE_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "claude_subscription": "Claude subscription",
        "claude_api_key": "Claude API key",
        "web_search": "Web search",
        "fake": "Fake provider",
    }
)
# Web searches have a budget of their own, whichever provider runs them (ADR-076 item 4).
WEB_SEARCH_SERVICE: Final[ServiceId] = "web_search"
# The service a provider kind's requests count on: both subscription sign-ins share one.
SERVICE_BY_PROVIDER_KIND: Final[Mapping[str, ServiceId]] = MappingProxyType(
    {
        "claude_subscription_token": "claude_subscription",
        "claude_existing_login": "claude_subscription",
        "claude_api_key": "claude_api_key",
        "fake": "fake",
    }
)
# The task type recorded for the connection test (ADR-076 item 3).
CONNECTION_TEST_TASK_TYPE: Final = "connection_test"
# Largest value an SQLite INTEGER holds; larger counts are refused, never clamped.
SQLITE_INTEGER_MAX: Final = 2**63 - 1

# A task type is an id, never prose: it is stored and shown in the breakdown.
_TASK_TYPE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def is_service(value: object) -> TypeGuard[ServiceId]:
    return isinstance(value, str) and value in SERVICE_NAMES


def is_task_type(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _TASK_TYPE.fullmatch(value) is not None


def is_stored_count(value: object) -> bool:
    """A non-negative int that fits an SQLite INTEGER (bools refused)."""
    return (
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= SQLITE_INTEGER_MAX
    )
