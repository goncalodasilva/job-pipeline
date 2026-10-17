"""Equivalent quantities preserve their sign and currency (ADR-089 item 5)."""

import pytest

from app.domain.honesty import Claim, new_claims


@pytest.mark.parametrize(
    ("suggested", "source", "claim"),
    [
        ("Saved €500.", "Saved $500.", "€500"),
        ("Saved GBP 500.", "Saved EUR 500.", "GBP 500"),
        ("Changed by 5%.", "Changed by -5%.", "5%"),
        ("Changed by -5%.", "Changed by 5%.", "-5%"),
        ("Changed by 5%.", "Changed by \u22125%.", "5%"),
        ("Changed by \u22125%.", "Changed by 5%.", "\u22125%"),
        ("Saved 500 euros.", "Saved 500 dollars.", "500 euros"),
        ("Saved usd 500.", "Saved gbp 500.", "usd 500"),
        ("Saved €5.", "Saved €\u22125.", "€5"),
    ],
)
def test_changed_currency_or_sign_is_a_new_claim(suggested: str, source: str, claim: str) -> None:
    kind = "percent" if "%" in suggested else "money"
    assert new_claims(suggested, [source]) == (Claim(kind, claim),)


@pytest.mark.parametrize(
    ("suggested", "source"),
    [
        ("Saved €500.", "Saved EUR 500."),
        ("Changed by -5 percent.", "Changed by -5%."),
        ("Changed by \u22125 percent.", "Changed by -5%."),
        ("Saved 500 euros.", "Saved EUR 500."),
        ("Saved £500.", "Saved 500 pounds."),
        ("Saved 500 dollars.", "Saved usd 500."),
    ],
)
def test_equivalent_currency_and_signed_formats_are_allowed(suggested: str, source: str) -> None:
    assert new_claims(suggested, [source]) == ()
