"""The multipart sink holds a part's headers in memory while they are read (M9.1 part D; TD-033;
spec 2.9 rule 5): it refuses a header name or value past 8 KiB itself, whatever limit the parser
library applies, however the bytes are cut up. Fictional values; no network."""

import pytest

from app.api.restore_upload import HEADER_BYTES, UploadRefused, _Sink


def sink() -> _Sink:
    return _Sink(None, 1024, frozenset())


def test_header_limit_is_8_kib() -> None:
    assert HEADER_BYTES == 8 * 1024


def test_a_header_value_up_to_the_limit_is_accepted_in_pieces() -> None:
    held = sink()
    for _ in range(8):
        held._header_value(b"a" * 1024, 0, 1024)  # 8 KiB exactly


def test_a_header_value_past_the_limit_is_refused_even_in_small_pieces() -> None:
    held = sink()
    with pytest.raises(UploadRefused) as caught:
        for _ in range(9):
            held._header_value(b"a" * 1024, 0, 1024)
    assert caught.value.reason == "form"


def test_a_header_name_past_the_limit_is_refused_even_in_small_pieces() -> None:
    held = sink()
    with pytest.raises(UploadRefused) as caught:
        for _ in range(9):
            held._header_field(b"a" * 1024, 0, 1024)
    assert caught.value.reason == "form"


def test_the_count_starts_again_with_each_header() -> None:
    held = sink()
    for _ in range(20):
        held._header_field(b"x-name", 0, 6)
        held._header_value(b"a" * 4000, 0, 4000)
        held._header_end()
