"""Content codings decoded within the size cap (M4.1; spec 2.9 rule 6).

``BoundedBody`` is fed raw chunks of any size, including single bytes and empty chunks.
Every value is fictional.
"""

import gzip
import zlib

import pytest

from app.domain.errors import FetchRefused
from app.modules.fetch.codings import BoundedBody, coding_of

PAGE = b"<p>Fictional Widget Engineer, Northwind Fictional Labs.</p>" * 20


def _deflate(data: bytes, wbits: int) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, wbits)
    return compressor.compress(data) + compressor.flush()


@pytest.mark.parametrize("wbits", [zlib.MAX_WBITS, -zlib.MAX_WBITS], ids=["zlib", "raw"])
def test_deflate_fed_one_byte_at_a_time_decodes(wbits: int) -> None:
    body = BoundedBody(coding_of("deflate"), max_bytes=len(PAGE))
    for byte in _deflate(PAGE, wbits):
        body.feed(b"")
        body.feed(bytes([byte]))
    assert body.finish() == PAGE


def test_short_or_empty_compressed_bodies() -> None:
    # Nothing at all with a coding declared: an empty body.
    assert BoundedBody("gzip", max_bytes=10).finish() == b""
    assert BoundedBody("deflate", max_bytes=10).finish() == b""

    # One byte of deflate never gets past the header: cut short.
    body = BoundedBody("deflate", max_bytes=10)
    body.feed(b"\x78")
    with pytest.raises(FetchRefused):
        body.finish()

    # Exactly at the cap is fine, one byte over is refused, whichever the coding.
    for coding, data in (("gzip", gzip.compress(PAGE)), ("identity", PAGE)):
        exact = BoundedBody(coding, max_bytes=len(PAGE))
        exact.feed(data)
        assert exact.finish() == PAGE
        over = BoundedBody(coding, max_bytes=len(PAGE) - 1)
        with pytest.raises(FetchRefused):
            over.feed(data)


def test_coding_names() -> None:
    assert coding_of(None) == coding_of("") == coding_of(" , ") == "identity"
    assert coding_of("X-GZIP") == "gzip"
    assert coding_of(" deflate ") == "deflate"
