"""Launch token generation (M0.14, spec 2.9 rule 2)."""

import base64
import re
from datetime import UTC, datetime
from pathlib import Path

from app.api.security import TOKEN_HEADER, generate_launch_token
from app.config import RuntimeConfig
from app.main import create_app
from tests.fakes.clock import FrozenClock

URLSAFE = re.compile(r"^[A-Za-z0-9_-]+$")
START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)


def _decoded_length(token: str) -> int:
    padded = token + "=" * (-len(token) % 4)
    return len(base64.urlsafe_b64decode(padded))


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=8000,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
    )


def test_launch_token_32_bytes_urlsafe_unique_per_app(tmp_path: Path) -> None:
    tokens = [generate_launch_token() for _ in range(20)]
    for token in tokens:
        assert URLSAFE.match(token), "token must use only the URL-safe alphabet, no padding"
        assert _decoded_length(token) == 32
    assert len(set(tokens)) == len(tokens)

    first = create_app(_config(tmp_path), clock=FrozenClock(START))
    second = create_app(_config(tmp_path), clock=FrozenClock(START))
    for app in (first, second):
        token = app.state.launch_token
        assert isinstance(token, str)
        assert URLSAFE.match(token)
        assert _decoded_length(token) == 32
    assert first.state.launch_token != second.state.launch_token


def test_token_header_name() -> None:
    # htmx sends this exact name through hx-headers (spec 2.9 rule 2).
    assert TOKEN_HEADER == "X-JP-Token"  # noqa: S105 - a header name, not a secret
