"""M4.4 controlled values and employer identity are mechanical, provider-free."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.job_normalization import company_key, normalize_value, value_key


@pytest.mark.parametrize(
    "given,expected",
    [
        ("RÉMÔTE", "remote"),
        (" fully  remote ", "remote"),
        ("Hybrid", "hybrid"),
        ("Unknown", "Unknown"),
    ],
)
def test_ac_m4_4_alias_normalizes_to_controlled_value(given, expected):
    assert normalize_value(given, {"remote": ["fully remote"], "hybrid": []}) == expected


def test_company_key_ignores_case_punctuation_legal_suffix():
    assert company_key("Harbor Kite, Ltd.") == company_key("HARBOR KITE LLC") == "harborkite"


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=30))
def test_normalize_ignores_case_space_accents_and_is_idempotent(text):
    expected = value_key(text)
    assert value_key("  " + " ".join(text.upper()) + "  ") == expected
    assert value_key(value_key(text)) == expected
    assert value_key("café") == value_key("CAFE")


@pytest.mark.parametrize("name", ["Harbor Kite L.L.C.", "Harbor Kite S.A.", "Harbor Kite, Ltd."])
def test_dotted_legal_suffixes(name):
    assert company_key(name) == company_key("Harbor Kite LLC")
