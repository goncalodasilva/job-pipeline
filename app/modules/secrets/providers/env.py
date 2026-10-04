"""Read-only fallback to environment variables, for systems without a keychain (spec 2.8, 7.3).

The app never writes these variables; the setup guide explains how to set them.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from app.domain.errors import SecretStoreUnavailable
from app.modules.secrets import ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN

ENVIRONMENT_VARIABLES: Final[Mapping[str, str]] = MappingProxyType(
    {
        ANTHROPIC_API_KEY: "ANTHROPIC_API_KEY",
        CLAUDE_CODE_OAUTH_TOKEN: "CLAUDE_CODE_OAUTH_TOKEN",
    }
)


class EnvSecretStore:
    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ  # read at call time, never copied

    @property
    def available(self) -> bool:
        return False  # nothing can be stored here

    def get(self, key: str) -> str | None:
        variable = ENVIRONMENT_VARIABLES.get(key)
        if variable is None:
            return None
        value = self._environ.get(variable, "").strip()
        return value or None

    def set(self, key: str, value: str) -> None:
        raise SecretStoreUnavailable()

    def delete(self, key: str) -> None:
        raise SecretStoreUnavailable()
