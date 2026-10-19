"""Content codings of fetched bodies, decoded within the size cap (spec 2.9 rule 6).

Stdlib ``zlib`` only. The guarded fetcher asks for ``ACCEPT_ENCODING`` and decodes the raw
body here rather than letting httpx do it, because httpx inflates a whole chunk (and every
stacked coding) before anyone can count the bytes: a few hundred bytes on the wire can
become gigabytes. Here each ``decompress`` call may produce at most one byte more than the
cap still allows, so decoded output in memory never passes ``max_bytes + 1``.

Exactly one coding is accepted: ``identity``, ``gzip`` (or its alias ``x-gzip``) or
``deflate``. Deflate is read as zlib-wrapped when its first two bytes are a valid zlib
header and as raw deflate otherwise, as browsers do. Stacked, unrequested (``br``,
``zstd``) and unknown codings, bad or truncated compressed data and bodies over the cap all
raise ``FetchRefused``.
"""

import zlib
from typing import Final, Literal

from app.domain.errors import FetchRefused

__all__ = ["ACCEPT_ENCODING", "BoundedBody", "Coding", "coding_of"]

Coding = Literal["identity", "gzip", "deflate"]

ACCEPT_ENCODING: Final = "gzip, deflate"
_ALIASES: Final[dict[str, Coding]] = {
    "identity": "identity",
    "gzip": "gzip",
    "x-gzip": "gzip",
    "deflate": "deflate",
}
_GZIP_WBITS: Final = 16 + zlib.MAX_WBITS
_ZLIB_WBITS: Final = zlib.MAX_WBITS
_RAW_DEFLATE_WBITS: Final = -zlib.MAX_WBITS
_ZLIB_HEADER_BYTES: Final = 2


def coding_of(header: str | None) -> Coding:
    """The one content coding of a ``Content-Encoding`` header (none means identity);
    raises ``FetchRefused`` for stacked, unrequested or unknown codings."""
    codings = [part.strip().lower() for part in (header or "").split(",") if part.strip()]
    if not codings:
        return "identity"
    if len(codings) > 1:
        raise FetchRefused(detail="stacked content codings")
    coding = _ALIASES.get(codings[0])
    if coding is None:
        raise FetchRefused(detail="content coding not allowed")
    return coding


class BoundedBody:
    """Collects a body's decoded bytes, refusing it once they pass ``max_bytes``."""

    def __init__(self, coding: Coding, max_bytes: int) -> None:
        self._coding = coding
        self._max_bytes = max_bytes
        self._body = bytearray()
        self._pending = b""  # deflate bytes held until the header can be read
        self._fed = False
        self._inflater: zlib._Decompress | None = (
            zlib.decompressobj(_GZIP_WBITS) if coding == "gzip" else None
        )

    def feed(self, chunk: bytes) -> None:
        """Add one raw chunk."""
        if not chunk:
            return
        self._fed = True
        if self._coding == "identity":
            self._append(chunk)
            return
        if self._inflater is None:  # deflate, before its first two bytes
            chunk = self._start_deflate(self._pending + chunk)
        if self._inflater is not None:
            self._inflate(self._inflater, chunk)

    def finish(self) -> bytes:
        """The decoded body; raises ``FetchRefused`` for compressed data cut short."""
        compressed = self._coding != "identity" and self._fed
        if compressed and (self._inflater is None or not self._inflater.eof):
            raise FetchRefused(detail="truncated compressed body")
        return bytes(self._body)

    def _start_deflate(self, data: bytes) -> bytes:
        if len(data) < _ZLIB_HEADER_BYTES:
            self._pending = data
            return b""
        self._pending = b""
        wrapped = (data[0] & 0x0F) == 8 and ((data[0] << 8) | data[1]) % 31 == 0
        self._inflater = zlib.decompressobj(_ZLIB_WBITS if wrapped else _RAW_DEFLATE_WBITS)
        return data

    def _inflate(self, inflater: "zlib._Decompress", data: bytes) -> None:
        try:
            while data and not inflater.eof:
                room = self._max_bytes - len(self._body)
                self._append(inflater.decompress(data, room + 1))
                data = inflater.unconsumed_tail
            # A call cut short by max_length can leave output pending with no input left.
            while not inflater.eof:
                room = self._max_bytes - len(self._body)
                pending = inflater.decompress(b"", room + 1)
                if not pending:
                    break
                self._append(pending)
        except zlib.error:
            raise FetchRefused(detail="undecodable body") from None
        # Anything after the end of the compressed stream is ignored.

    def _append(self, data: bytes) -> None:
        if len(self._body) + len(data) > self._max_bytes:
            raise FetchRefused(detail="page too large")
        self._body += data
