"""Pure secret redaction helper (spec 2.9 rule 9; task M0.10; ADR-030).

Key-shaped strings are built at runtime from parts, never written as literals, so secret
scanners on commits see no token-shaped text. Every value here is fictional.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.redaction import REDACTED, Redactor

FICTIONAL_BODY = "FICTIONALtestOnly0123456789_abc-XYZ"


def anthropic_shaped(kind: str, body: str = FICTIONAL_BODY) -> str:
    """Build an Anthropic-shaped token (``sk-ant-<kind>-<body>``) at runtime."""
    return "-".join(["sk", "ant", kind, body])


def test_redacted_marker_value() -> None:
    assert REDACTED == "[REDACTED]"


def test_registered_secret_replaced_everywhere_in_text() -> None:
    redactor = Redactor()
    hidden = "fictional-hidden-value-42"
    redactor.register(hidden)

    out = redactor.redact(f"first {hidden} then {hidden}!")

    assert hidden not in out
    assert out == f"first {REDACTED} then {REDACTED}!"


def test_text_without_secrets_is_unchanged() -> None:
    redactor = Redactor()
    redactor.register("fictional-secret-value-42")
    text = "Nothing to hide here: job 17 saved."

    assert redactor.redact(text) == text


def test_empty_secret_is_ignored() -> None:
    redactor = Redactor()
    redactor.register("")
    text = "plain text stays plain"

    assert redactor.redact(text) == text


def test_overlapping_secrets_are_fully_removed_longest_first() -> None:
    redactor = Redactor()
    short = "fictionalPass"
    long = "fictionalPassWithSuffix99"
    redactor.register(short)
    redactor.register(long)

    out = redactor.redact(f"value={long}")

    assert short not in out
    assert "WithSuffix99" not in out
    assert out == f"value={REDACTED}"


@pytest.mark.parametrize("kind", ["api03", "oat01"])
def test_ac_m0_5_anthropic_key_and_oauth_shapes_redacted(kind: str) -> None:
    redactor = Redactor()  # nothing registered: the shape alone is enough
    token = anthropic_shaped(kind)

    out = redactor.redact(f"auth header was Bearer {token} end")

    assert token not in out
    assert FICTIONAL_BODY not in out
    assert out == f"auth header was Bearer {REDACTED} end"


def test_ordinary_sk_prefixed_words_are_not_redacted() -> None:
    redactor = Redactor()
    text = "task-ant-colony and sk-ant are just words"

    assert redactor.redact(text) == text


def test_instances_do_not_share_secrets() -> None:
    first = Redactor()
    second = Redactor()
    hidden = "fictional-only-in-first-77"
    first.register(hidden)

    assert hidden not in first.redact(f"x {hidden} y")
    assert second.redact(f"x {hidden} y") == f"x {hidden} y"


@given(
    secret=st.text(
        alphabet=st.characters(categories=["Lu", "Ll", "Nd"], max_codepoint=127),
        min_size=9,  # longer than "REDACTED", so it can't hide inside the marker
        max_size=40,
    ),
    before=st.text(max_size=60),
    after=st.text(max_size=60),
)
def test_ac_m0_5_secret_embedded_in_arbitrary_text_redacted(
    secret: str, before: str, after: str
) -> None:
    redactor = Redactor()
    redactor.register(secret)

    out = redactor.redact(before + secret + after)

    assert secret not in out
    assert REDACTED in out


@given(
    secret=st.text(min_size=12, max_size=40).filter(lambda s: "REDACTED" not in s.upper()),
)
def test_escaped_forms_of_a_registered_secret_are_redacted(secret: str) -> None:
    import json
    from urllib.parse import quote

    redactor = Redactor()
    redactor.register(secret)

    for shown in (
        repr(secret),
        json.dumps(secret),
        repr(secret.encode("utf-8")),
        quote(secret, safe=""),
        repr({"key": secret}),
    ):
        assert secret not in redactor.redact(shown)
        assert REDACTED in redactor.redact(shown)


def test_token_shape_is_case_insensitive() -> None:
    token = anthropic_shaped("api03").upper()

    assert Redactor().redact(f"x {token} y") == f"x {REDACTED} y"
