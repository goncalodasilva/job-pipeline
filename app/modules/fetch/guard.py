"""The SSRF guard for job page fetches (spec 2.9 rule 6; ADR-020, ADR-036, ADR-098 item 2).

Stdlib only: the guarded fetcher in ``providers/guarded.py`` is the one ``httpx`` user and
calls ``check_url`` before every hop, then connects to the address it answered.

``parse_url`` accepts only http and https URLs with a plain host: no userinfo, no
backslashes, whitespace or control characters, and no numeric host spelling other than a
canonical dotted quad or a bracketed IPv6 literal (decimal, hex, octal and short forms are
refused, because other parsers read them as addresses). Internationalized names become
their ASCII (IDNA) form.

``check_url`` resolves the host through an injected ``Resolver`` (an IP literal is never
resolved) and refuses the URL unless every answer is global unicast. Which addresses count
is fixed by the explicit tables below rather than ``ipaddress``'s ``is_global``, so every
Python version agrees. The URL itself is never put into an error's ``detail``.
"""

import ipaddress
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import SplitResult, urlsplit

from app.domain.errors import FetchRefused

__all__ = [
    "CheckedUrl",
    "ParsedUrl",
    "Resolver",
    "check_url",
    "is_global_unicast",
    "parse_url",
]

# A host name to its addresses, as strings ("93.184.215.14", "2a00:1450::200e").
Resolver = Callable[[str], Awaitable[Sequence[str]]]

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

DEFAULT_PORTS: Final = {"http": 80, "https": 443}

# IPv4 ranges that are never fetched: "this network", private, shared (CGNAT), loopback,
# link-local, IETF protocol assignments, documentation, 6to4 relay anycast, benchmarking,
# multicast and reserved (with the limited broadcast).
REFUSED_V4: Final = tuple(
    ipaddress.IPv4Network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
# IPv6 is fetched only inside global unicast 2000::/3. Loopback, unspecified, IPv4-mapped,
# NAT64, unique-local, link-local and multicast all lie outside it. Inside it, these are
# refused: IETF protocol assignments (Teredo among them), documentation, 6to4 (which can
# embed any IPv4 address) and the newer documentation range.
GLOBAL_V6: Final = ipaddress.IPv6Network("2000::/3")
REFUSED_V6: Final = tuple(
    ipaddress.IPv6Network(network)
    for network in ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20")
)

# A canonical dotted quad: four decimal parts from 0 to 255 without leading zeros.
_OCTET: Final = r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])"
_DOTTED_QUAD: Final = re.compile(rf"{_OCTET}(?:\.{_OCTET}){{3}}")
# Host names after IDNA: letters, digits, hyphens and underscores, in dot-separated labels.
_HOST_NAME: Final = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*")
# A label other URL parsers read as a number (decimal, octal or hex): a host ending in one
# is an IPv4 address to them.
_NUMERIC_LABEL: Final = re.compile(r"[0-9]+|0x[0-9a-f]*")


@dataclass(frozen=True)
class ParsedUrl:
    """An accepted URL: lower-case scheme, lower-case ASCII host without brackets, the port
    (the scheme's default when none is written) and the URL as given."""

    scheme: str
    host: str
    port: int
    url: str

    @property
    def default_port(self) -> bool:
        return self.port == DEFAULT_PORTS[self.scheme]


@dataclass(frozen=True)
class CheckedUrl:
    """A URL whose host resolved only to global unicast addresses; ``address`` is the one to
    connect to (the resolver's first answer, or the literal itself)."""

    parsed: ParsedUrl
    address: str


def is_global_unicast(address: IPAddress) -> bool:
    """Whether ``address`` may be fetched, from the explicit tables above."""
    if isinstance(address, ipaddress.IPv4Address):
        return not any(address in network for network in REFUSED_V4)
    if address.scope_id is not None:
        return False
    return address in GLOBAL_V6 and not any(address in network for network in REFUSED_V6)


def parse_url(url: str) -> ParsedUrl:
    """Check the shape of ``url``; raises ``FetchRefused`` for anything but a plain http(s)
    URL."""
    if not url or any(_forbidden_character(character) for character in url):
        raise FetchRefused(detail="malformed address")
    parts, port = _split(url)
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise FetchRefused(detail="scheme not allowed")
    if "@" in parts.netloc:
        raise FetchRefused(detail="userinfo not allowed")
    host = _host(parts)
    return ParsedUrl(
        scheme=scheme,
        host=host,
        port=port if port is not None else DEFAULT_PORTS[scheme],
        url=url,
    )


async def check_url(url: str, resolver: Resolver) -> CheckedUrl:
    """Parse ``url`` and resolve its host; raises ``FetchRefused`` unless every address is
    global unicast. An IP literal is checked as it is, without a lookup."""
    parsed = parse_url(url)
    literal = _ip_or_none(parsed.host)
    if literal is not None:
        answers: list[IPAddress] = [literal]
    else:
        answers = await _resolve(parsed.host, resolver)
    if not answers:
        raise FetchRefused(detail="unknown host")
    if not all(is_global_unicast(address) for address in answers):
        raise FetchRefused(detail="address not allowed")
    return CheckedUrl(parsed=parsed, address=str(answers[0]))


def _forbidden_character(character: str) -> bool:
    code = ord(character)
    return character == "\\" or character.isspace() or code < 0x20 or code == 0x7F


def _split(url: str) -> tuple[SplitResult, int | None]:
    try:
        parts = urlsplit(url)
        return parts, parts.port
    except ValueError:
        raise FetchRefused(detail="malformed address") from None


def _host(parts: SplitResult) -> str:
    """The host in canonical form, or ``FetchRefused``."""
    raw = parts.hostname
    if not raw:
        raise FetchRefused(detail="missing host")
    if parts.netloc.startswith("["):
        return _ipv6_literal(raw)
    try:
        host = raw.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise FetchRefused(detail="malformed host") from None
    if not _HOST_NAME.fullmatch(host):
        raise FetchRefused(detail="malformed host")
    last_label = host.rsplit(".", 1)[-1]
    if _NUMERIC_LABEL.fullmatch(last_label) and not _DOTTED_QUAD.fullmatch(host):
        raise FetchRefused(detail="numeric host spelling")
    return host


def _ipv6_literal(raw: str) -> str:
    """A bracketed host: an IPv6 address without a zone ("%eth0")."""
    address = _ip_or_none(raw)
    if not isinstance(address, ipaddress.IPv6Address) or address.scope_id is not None:
        raise FetchRefused(detail="malformed host")
    return str(address)


def _ip_or_none(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


async def _resolve(host: str, resolver: Resolver) -> list[IPAddress]:
    try:
        found = await resolver(host)
    except OSError:  # socket.gaierror included
        raise FetchRefused(detail="unknown host") from None
    answers: list[IPAddress] = []
    for text in found:
        address = _ip_or_none(text)
        if address is None:
            raise FetchRefused(detail="unusable address")
        answers.append(address)
    return answers
