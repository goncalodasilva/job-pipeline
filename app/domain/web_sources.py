"""Checks shared by everything the app keeps from a web search: text, dates and source urls.

Pure and stdlib-only. Search results are untrusted (spec 3.3), so what a model reports from them
is reduced to short plain text and http(s) links before anything is stored or shown.
"""

from urllib.parse import urlsplit


def _hidden(c: str) -> bool:
    """Control, line-separator and bidirectional-mark characters that must never be stored."""
    n = ord(c)
    return (
        n < 32
        or 0x7F <= n <= 0x9F
        or n in {0x061C, 0x200E, 0x200F, 0x2028, 0x2029}
        or 0x202A <= n <= 0x202E
        or 0x2066 <= n <= 0x2069
    )


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        return ""
    if any(_hidden(c) for c in value):
        return ""
    return value.strip()


def _source_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()[:100]
    return text if text and not any(_hidden(c) for c in text) else None


def _safe_url(url: str) -> bool:
    if len(url) > 2000 or any(_hidden(c) or c.isspace() for c in url):
        return False
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and "@" not in parsed.netloc
        )
    except ValueError:
        return False
