"""Job intake's pure text and link rules (M4.3)."""

import unicodedata
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.domain.errors import JobLinkInvalid, JobTextNotFound, JobTextTooLong

MAX_JOB_TEXT: Final = 50_000
DUPLICATE_AT: Final = 0.80
_TRACKING: Final = frozenset({"fbclid", "gclid", "dclid", "msclkid", "ref", "referrer"})


def clean_job_text(text: str) -> str:
    # Refuse before cleaning too: invisible characters must not bypass the input cap.
    if len(text) > MAX_JOB_TEXT:
        raise JobTextTooLong()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "".join(
        c for c in text if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"}
    ).strip()
    if not cleaned:
        raise JobTextNotFound()
    return cleaned


def checked_link(url: str) -> str:
    if len(url) > 8192 or any(c.isspace() or unicodedata.category(c).startswith("C") for c in url):
        raise JobLinkInvalid()
    try:
        parts = urlsplit(url)
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            raise JobLinkInvalid()
        _ = parts.port
        parts.hostname.encode("idna")
    except (ValueError, UnicodeError):
        raise JobLinkInvalid() from None
    return parts.scheme.lower() + url[url.index(":") :]


def canonical_url(url: str) -> str:
    parts = urlsplit(checked_link(url))
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").encode("idna").decode("ascii").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    authority = (
        host
        if port is None or (scheme, port) in {("http", 80), ("https", 443)}
        else f"{host}:{port}"
    )
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.casefold().startswith("utm_") and k.casefold() not in _TRACKING
    )
    return urlunsplit((scheme, authority, parts.path or "/", urlencode(query), ""))
