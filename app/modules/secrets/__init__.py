"""Secrets module interface (spec 2.4, 2.9.9, ADR-007).

Implementations (M1): KeyringSecretStore, EnvSecretStore (read-only fallback) and a memory
store for tests. Secret values never go to the database, logs, responses or exports.
"""

from dataclasses import dataclass
from typing import Final, Literal, Protocol

ANTHROPIC_API_KEY: Final = "anthropic_api_key"
CLAUDE_CODE_OAUTH_TOKEN: Final = "claude_code_oauth_token"  # noqa: S105 - a key name, not a secret
KNOWN_SECRET_KEYS: Final = (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN)

SecretSource = Literal["keychain", "environment"]


@dataclass(frozen=True)
class SecretLookup:
    """One read of a secret: where it came from, and whether the keychain failed to answer
    (so the interface can say "Keychain couldn't be read; using the environment variable")."""

    value: str | None
    source: SecretSource | None
    keychain_failed: bool = False

    def __repr__(self) -> str:  # never show the value
        return f"SecretLookup(source={self.source!r}, keychain_failed={self.keychain_failed!r})"


class SecretStore(Protocol):
    @property
    def available(self) -> bool:
        """False when no keychain is usable (the setup guide then explains the fallback)."""
        ...

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...
