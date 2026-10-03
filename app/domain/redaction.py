"""Secret redaction for log output (spec 2.9 rule 9; ADR-030).

A ``Redactor`` replaces the secret values registered with it, and any string shaped like an
Anthropic API key or OAuth token, with ``REDACTED``. Each instance holds its own secrets.
Escaped copies of a secret (``repr``, JSON, bytes and URL-encoded forms) are registered too,
because log arguments, extras and tracebacks often show values in those forms.
"""

import json
import re
import threading
from urllib.parse import quote

REDACTED = "[REDACTED]"

# Anthropic keys and OAuth tokens: sk-ant-<kind><version>-<body>, e.g. kinds api03 and oat01.
_TOKEN_SHAPE = re.compile(r"sk-ant-[a-z]+\d+-[A-Za-z0-9_-]+", re.IGNORECASE)


def _forms(secret: str) -> set[str]:
    return {
        secret,
        repr(secret)[1:-1],
        json.dumps(secret)[1:-1],
        repr(secret.encode("utf-8"))[2:-1],
        quote(secret, safe=""),
    }


class Redactor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: frozenset[str] = frozenset()
        self._pattern: re.Pattern[str] | None = None

    def register(self, secret: str) -> None:
        if not secret:
            return
        with self._lock:
            secrets = self._secrets | {form for form in _forms(secret) if form}
            if secrets == self._secrets:
                return
            # Longest first, so a secret that contains another is removed whole.
            ordered = sorted(secrets, key=len, reverse=True)
            self._pattern = re.compile("|".join(re.escape(value) for value in ordered))
            self._secrets = secrets

    def redact(self, text: str) -> str:
        pattern = self._pattern
        if pattern is not None:
            text = pattern.sub(REDACTED, text)
        return _TOKEN_SHAPE.sub(REDACTED, text)
