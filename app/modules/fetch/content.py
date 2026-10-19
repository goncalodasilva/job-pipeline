"""What every job page fetcher shares: limits, content types and decoding (spec 2.9 rule 6).

Stdlib only, so the guarded fetcher and the fake treat a page the same way. Limits are
clamped to the ``FetchLimits()`` maximums: callers can only lower them (ADR-036). Only text
content types are accepted. A body is decoded with the charset in the ``Content-Type``
header, then a ``<meta>`` charset in its first 2 KB, then UTF-8; bytes that don't decode
become replacement characters.
"""

import re
from dataclasses import dataclass
from typing import Final

from app.domain.errors import FetchRefused
from app.modules.fetch import FetchLimits

__all__ = ["MAXIMUM_LIMITS", "MediaType", "clamp_limits", "decode_body", "parse_content_type"]

MAXIMUM_LIMITS: Final = FetchLimits()
META_WINDOW_BYTES: Final = 2048
_TEXT_TYPES: Final = frozenset({"application/xhtml+xml"})
_META_CHARSET: Final = re.compile(rb"<meta[^>]*?charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", re.I)


@dataclass(frozen=True)
class MediaType:
    """A ``Content-Type`` header: the lower-case media type and the declared charset."""

    media_type: str
    charset: str | None


def clamp_limits(limits: FetchLimits) -> FetchLimits:
    """``limits`` lowered to the spec 2.9.6 maximums where they ask for more."""
    return FetchLimits(
        max_bytes=min(limits.max_bytes, MAXIMUM_LIMITS.max_bytes),
        timeout_seconds=min(limits.timeout_seconds, MAXIMUM_LIMITS.timeout_seconds),
        max_redirects=max(0, min(limits.max_redirects, MAXIMUM_LIMITS.max_redirects)),
    )


def parse_content_type(header: str | None) -> MediaType:
    """The media type and charset of a text page; raises ``FetchRefused`` for a missing or
    non-text type."""
    if not header:
        raise FetchRefused(detail="no content type")
    media_type, *parameters = (part.strip() for part in header.split(";"))
    media_type = media_type.lower()
    if not (media_type.startswith("text/") or media_type in _TEXT_TYPES):
        raise FetchRefused(detail="content type not allowed")
    charset = None
    for parameter in parameters:
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "charset":
            charset = value.strip().strip("\"'") or None
    return MediaType(media_type=media_type, charset=charset)


def decode_body(body: bytes, charset: str | None) -> str:
    """``body`` as text: the declared charset, then a ``<meta>`` charset, then UTF-8."""
    for candidate in (charset, _meta_charset(body)):
        if candidate is None:
            continue
        try:
            return body.decode(candidate, errors="replace")
        except LookupError:  # unknown, or not a text encoding ("base64")
            continue
    return body.decode("utf-8", errors="replace")


def _meta_charset(body: bytes) -> str | None:
    found = _META_CHARSET.search(body[:META_WINDOW_BYTES])
    return found.group(1).decode("ascii") if found is not None else None
