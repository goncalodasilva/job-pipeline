"""The SSRF guard (M4.1; AC-M4.1; spec 2.9 rule 6; ADR-020, ADR-036, ADR-098 item 2).

``app.modules.fetch.guard`` (stdlib only, no ``httpx``):

- ``is_global_unicast(address)`` follows explicit tables, so every Python version agrees.
  IPv4 is accepted unless it is in one of ``REFUSED_V4`` below; IPv6 is accepted only inside
  2000::/3 and outside ``REFUSED_V6``. Loopback, unspecified, link-local, unique-local,
  multicast, IPv4-mapped and NAT64 IPv6 forms all fall outside 2000::/3; 6to4 is refused
  explicitly.
- ``parse_url(url)`` answers a ``ParsedUrl`` (``scheme``, lower-case ASCII ``host`` without
  brackets, ``port`` with the scheme default, ``url``) or raises ``FetchRefused``: only http
  and https, a host is required, no userinfo, no backslashes, whitespace or control
  characters, and no numeric host spelling other than a canonical dotted quad or a bracketed
  IPv6 literal.
- ``await check_url(url, resolver)`` answers a ``CheckedUrl`` (``parsed`` and the one
  ``address`` to connect to, the resolver's first answer). An IP literal is never resolved.
  The URL is refused when the resolver answers nothing, fails, or answers any address that is
  not global unicast.

The resolver is a fake; nothing touches the network. The new names are imported inside the
tests, so each one fails on its own until the module exists.
"""

import asyncio
import importlib
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.domain.errors import FetchRefused

# Global unicast addresses used as "public" answers. They are only numbers here: no test
# connects anywhere.
PUBLIC_V4 = "93.184.215.14"
PUBLIC_V4_OTHER = "151.101.1.69"
PUBLIC_V6 = "2a00:1450:4001:829::200e"
HOST = "jobs.example.com"

REFUSED_V4 = tuple(
    ipaddress.IPv4Network(net)
    for net in (
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
GLOBAL_V6 = ipaddress.IPv6Network("2000::/3")
REFUSED_V6 = tuple(
    ipaddress.IPv6Network(net) for net in ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20")
)
# Interesting IPv6 ranges outside 2000::/3, sampled so edges are hit often.
OTHER_V6 = tuple(
    ipaddress.IPv6Network(net)
    for net in (
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)

REFUSED_ADDRESSES = [
    "127.0.0.1",
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "100.64.0.1",
    "0.0.0.0",  # noqa: S104 - an address the guard must refuse, not a bind
    "224.0.0.1",
    "255.255.255.255",
    "::1",
    "::",
    "fe80::1",
    "fc00::1",
    "ff02::1",
    "::ffff:127.0.0.1",
    "64:ff9b::7f00:1",
    "2002:7f00:1::",
]


def _guard() -> Any:
    return importlib.import_module("app.modules.fetch.guard")


def _oracle(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """The accepted set, written from the tables in the module docstring."""
    if isinstance(address, ipaddress.IPv4Address):
        return not any(address in net for net in REFUSED_V4)
    return address in GLOBAL_V6 and not any(address in net for net in REFUSED_V6)


def _literal_url(address: str) -> str:
    return f"http://[{address}]/job" if ":" in address else f"http://{address}/job"


class FakeResolver:
    """Answers fixed addresses per host and records every lookup."""

    def __init__(self, answers: Mapping[str, Sequence[str]]) -> None:
        self._answers = answers
        self.calls: list[str] = []

    async def __call__(self, host: str) -> Sequence[str]:
        self.calls.append(host)
        return list(self._answers.get(host, ()))


def _failing_resolver(error: Exception) -> Callable[[str], Awaitable[Sequence[str]]]:
    async def resolve(host: str) -> Sequence[str]:
        raise error

    return resolve


# --- addresses ---------------------------------------------------------------------------


@pytest.mark.parametrize("address", REFUSED_ADDRESSES, ids=REFUSED_ADDRESSES)
async def test_ac_m4_1_refuses_address(address: str) -> None:
    guard = _guard()
    assert guard.is_global_unicast(ipaddress.ip_address(address)) is False

    # As a literal in the URL: refused without asking the resolver.
    literal_resolver = FakeResolver({})
    with pytest.raises(FetchRefused):
        await guard.check_url(_literal_url(address), literal_resolver)
    assert literal_resolver.calls == []

    # As a DNS answer for an ordinary-looking name.
    resolver = FakeResolver({HOST: [address]})
    with pytest.raises(FetchRefused):
        await guard.check_url(f"https://{HOST}/postings/1", resolver)
    assert resolver.calls == [HOST]


@pytest.mark.parametrize("address", [PUBLIC_V4, PUBLIC_V6])
async def test_public_address_accepted(address: str) -> None:
    guard = _guard()
    assert guard.is_global_unicast(ipaddress.ip_address(address)) is True

    checked = await guard.check_url(_literal_url(address), FakeResolver({}))
    assert checked.address == address
    assert checked.parsed.host == address

    resolver = FakeResolver({HOST: [address]})
    checked = await guard.check_url(f"https://{HOST}/postings/1?ref=feed", resolver)
    assert checked.address == address
    assert checked.parsed.host == HOST
    assert checked.parsed.scheme == "https"
    assert checked.parsed.port == 443
    assert resolver.calls == [HOST]


async def test_any_private_answer_in_dns_refuses_the_url() -> None:
    guard = _guard()
    url = f"https://{HOST}/postings/1"

    for answers in (
        [PUBLIC_V4, "10.0.0.7"],
        ["10.0.0.7", PUBLIC_V4],
        [PUBLIC_V6, "::1"],
        [PUBLIC_V4, PUBLIC_V6, "169.254.169.254"],
    ):
        with pytest.raises(FetchRefused):
            await guard.check_url(url, FakeResolver({HOST: answers}))

    # No answer at all, or a resolver failure: an unknown host, refused.
    with pytest.raises(FetchRefused):
        await guard.check_url(url, FakeResolver({HOST: []}))
    for error in (socket.gaierror(socket.EAI_NONAME, "unknown"), OSError("resolver down")):
        with pytest.raises(FetchRefused):
            await guard.check_url(url, _failing_resolver(error))

    # Only public answers: the first one is the address to connect to.
    checked = await guard.check_url(url, FakeResolver({HOST: [PUBLIC_V4, PUBLIC_V4_OTHER]}))
    assert checked.address == PUBLIC_V4


@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_guard_accepts_only_global_unicast(family: str, data: st.DataObject) -> None:
    guard = _guard()
    if family == "ipv4":
        strategy = st.one_of(
            st.ip_addresses(v=4),
            st.sampled_from(REFUSED_V4).flatmap(lambda net: st.ip_addresses(network=net)),
        )
    else:
        strategy = st.one_of(
            st.ip_addresses(v=6),
            st.ip_addresses(network=GLOBAL_V6),
            st.sampled_from(REFUSED_V6 + OTHER_V6).flatmap(
                lambda net: st.ip_addresses(network=net)
            ),
        )
    address = data.draw(strategy)
    expected = _oracle(address)

    assert guard.is_global_unicast(address) is expected
    # One way only: whatever the guard accepts, Python also calls global, and not multicast.
    if expected:
        assert address.is_global and not address.is_multicast, address

    # The same answer reached through a URL literal and through DNS.
    for url, resolver in (
        (_literal_url(str(address)), FakeResolver({})),
        (f"http://{HOST}/job", FakeResolver({HOST: [str(address)]})),
    ):
        if expected:
            assert asyncio.run(guard.check_url(url, resolver)).address == str(address)
        else:
            with pytest.raises(FetchRefused):
                asyncio.run(guard.check_url(url, resolver))


# --- URL shapes ---------------------------------------------------------------------------

SCHEME_CASES = {
    "file": "file:///etc/hosts",
    "ftp": f"ftp://{HOST}/postings/1",
    "javascript": "javascript:alert(document.domain)",
    "data": "data:text/html,<p>Fictional</p>",
    "gopher": f"gopher://{HOST}/1",
    "ws": f"ws://{HOST}/socket",
    "none": f"{HOST}/postings/1",
}


@pytest.mark.parametrize("case", sorted(SCHEME_CASES), ids=sorted(SCHEME_CASES))
async def test_ac_m4_1_refuses_scheme(case: str) -> None:
    guard = _guard()
    url = SCHEME_CASES[case]

    with pytest.raises(FetchRefused):
        guard.parse_url(url)
    resolver = FakeResolver({HOST: [PUBLIC_V4]})
    with pytest.raises(FetchRefused):
        await guard.check_url(url, resolver)
    assert resolver.calls == []


@pytest.mark.parametrize("scheme", ["HTTP", "Https"])
def test_scheme_matching_ignores_case(scheme: str) -> None:
    parsed = _guard().parse_url(f"{scheme}://{HOST}/postings/1")
    assert parsed.scheme == scheme.lower()


NUMERIC_SPELLINGS = ["2130706433", "0x7f000001", "0177.0.0.1", "127.1"]


@pytest.mark.parametrize("spelling", NUMERIC_SPELLINGS, ids=NUMERIC_SPELLINGS)
async def test_ac_m4_1_refuses_numeric_host_spellings(spelling: str) -> None:
    guard = _guard()
    url = f"http://{spelling}/admin"

    with pytest.raises(FetchRefused):
        guard.parse_url(url)
    # Even a resolver that would call it public can't make it acceptable.
    resolver = FakeResolver({spelling: [PUBLIC_V4]})
    with pytest.raises(FetchRefused):
        await guard.check_url(url, resolver)
    assert resolver.calls == []


MALFORMED_URLS = {
    "userinfo": f"http://user@{HOST}/",
    "userinfo_password": f"https://user:secret@{HOST}/",
    "userinfo_host_trick": "http://jobs.example.com@10.0.0.1/",
    "userinfo_empty": f"http://@{HOST}/",
    "backslash": f"http://{HOST}\\@10.0.0.1/",
    "backslash_path": f"https://{HOST}/a\\b",
    "space": f"http://{HOST}/a b",
    "tab": f"http://{HOST}/\tpath",
    "newline": f"http://{HOST}/\npath",
    "control": f"http://{HOST}/\x00path",
    "missing_host": "http:///postings/1",
    "empty": "",
    "hex_short": "http://0x7f.1/",
    "octal_part": "http://127.0.0.01/",
    "decimal_three_part": "http://127.0.1/",
    "unbracketed_ipv6": "http://::1/",
}


@pytest.mark.parametrize("case", sorted(MALFORMED_URLS), ids=sorted(MALFORMED_URLS))
async def test_ac_m4_1_refuses_userinfo_and_malformed_urls(case: str) -> None:
    guard = _guard()
    url = MALFORMED_URLS[case]

    with pytest.raises(FetchRefused):
        guard.parse_url(url)
    resolver = FakeResolver({HOST: [PUBLIC_V4]})
    with pytest.raises(FetchRefused):
        await guard.check_url(url, resolver)
    assert resolver.calls == []


def test_parse_url_normalizes_host_and_port() -> None:
    guard = _guard()

    parsed = guard.parse_url("https://Jobs.Example.COM/Postings/1?Ref=Feed")
    assert (parsed.scheme, parsed.host, parsed.port) == ("https", "jobs.example.com", 443)

    parsed = guard.parse_url(f"http://{HOST}/")
    assert parsed.port == 80

    parsed = guard.parse_url(f"https://{HOST}:8443/x")
    assert parsed.port == 8443

    parsed = guard.parse_url("https://bücher.example.com/jobs")
    assert parsed.host == "xn--bcher-kva.example.com"

    parsed = guard.parse_url(f"http://[{PUBLIC_V6}]:8080/x")
    assert (parsed.host, parsed.port) == (PUBLIC_V6, 8080)

    parsed = guard.parse_url(f"http://{PUBLIC_V4}/x")
    assert parsed.host == PUBLIC_V4


def test_guard_module_imports_no_http_client() -> None:
    """The guard is stdlib only; ``httpx`` stays in the guarded provider (ADR-098 item 2)."""
    source = _guard().__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "import httpx" not in text
    assert "from httpx" not in text
