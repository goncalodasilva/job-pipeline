"""Shared fetch content helpers (M4.1; spec 2.9 rule 6): charset fallbacks.

An unknown or non-text charset name, declared in the header or a ``<meta>`` tag, falls back
to the next source and finally to UTF-8 with replacement characters. Every value is fictional.
"""

from app.modules.fetch.content import decode_body

CAFE = "Café Fictício"


def test_unknown_or_non_text_charset_falls_back() -> None:
    utf8 = CAFE.encode()
    latin1 = CAFE.encode("iso-8859-1")

    assert decode_body(utf8, "no-such-charset") == CAFE
    assert decode_body(utf8, "base64") == CAFE  # a codec, but not a text encoding
    meta = b'<meta charset="iso-8859-1">'
    assert decode_body(meta + latin1, "no-such-charset").endswith(CAFE)
    bad_meta = b'<meta charset="no-such-charset">'
    assert decode_body(bad_meta + latin1, None).endswith("Caf� Fict�cio")
