"""The setup route only follows a finished step's redirect to a path on this app (M1.19).

``_local_path`` keeps local paths and sends anything else (scheme-relative ``//host``, the
backslash form browsers treat the same, absolute URLs, relative text and any control
character, which browsers strip before resolving) to Today, ``/``.
"""

import pytest

from app.api.setup import _local_path


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        pytest.param("/inbox", "/inbox", id="inbox"),
        pytest.param("/", "/", id="root"),
        pytest.param("//x", "/", id="scheme-relative"),
        pytest.param("/\\x", "/", id="backslash"),
        pytest.param("https://x", "/", id="absolute-url"),
        pytest.param("x", "/", id="relative"),
        pytest.param("/\t/x", "/", id="tab"),
        pytest.param("/\r\n/x", "/", id="newline"),
        pytest.param("/inbox\x00", "/", id="nul"),
        pytest.param("/inbox\x7f", "/", id="delete"),
    ],
)
def test_local_path(target: str, expected: str) -> None:
    assert _local_path(target) == expected
