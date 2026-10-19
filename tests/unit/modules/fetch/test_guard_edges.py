"""Edge cases of the SSRF guard's defensive checks (M4.1; spec 2.9 rule 6).

Zoned IPv6 addresses, host names that IDNA can't encode or that hold characters other
parsers would decode (percent escapes), and resolver answers that aren't addresses are all
refused. The resolver is a fake; nothing touches the network.
"""

import ipaddress
from collections.abc import Sequence

import pytest

from app.domain.errors import FetchRefused
from app.modules.fetch.guard import check_url, is_global_unicast, parse_url

HOST = "jobs.example.com"
PUBLIC_V6 = "2a00:1450:4001:829::200e"


class FakeResolver:
    def __init__(self, answers: Sequence[str]) -> None:
        self._answers = answers
        self.calls: list[str] = []

    async def __call__(self, host: str) -> Sequence[str]:
        self.calls.append(host)
        return self._answers


def _answering(*answers: str) -> FakeResolver:
    return FakeResolver(list(answers))


async def test_zoned_ipv6_refused_as_literal_and_as_answer() -> None:
    assert is_global_unicast(ipaddress.ip_address(f"{PUBLIC_V6}%en0")) is False

    with pytest.raises(FetchRefused):
        parse_url(f"http://[{PUBLIC_V6}%25en0]/job")
    with pytest.raises(FetchRefused):
        await check_url(f"https://{HOST}/job", _answering(f"{PUBLIC_V6}%en0"))


@pytest.mark.parametrize(
    "url",
    [
        "http://jobs..example.com/",  # an empty label
        f"http://{'a' * 64}.example.com/",  # a label over 63 characters
        "http://exa%6dple.com/",  # a percent escape in the host
        "http://jobs!.example.com/",
        "http://jobs.example.com./",  # a trailing dot
    ],
)
def test_host_names_idna_or_the_host_pattern_refuse(url: str) -> None:
    with pytest.raises(FetchRefused):
        parse_url(url)


async def test_resolver_answer_that_is_not_an_address_refused() -> None:
    resolver = _answering("93.184.215.14", "not-an-address")

    with pytest.raises(FetchRefused):
        await check_url(f"https://{HOST}/job", resolver)
    assert resolver.calls == [HOST]
