"""Static country and currency lists for the job preferences step (M1.19; AC-M1.6).

``app/domain/locales.py`` ships two pure-data tuples used by the step's select controls and by
its validation (a well-shaped but unassigned code such as ``ZZ`` is refused):

- ``COUNTRIES``: ``(code, name)`` pairs for ISO 3166-1 alpha-2 (the full assigned list);
- ``CURRENCIES``: ``(code, name)`` pairs for ISO 4217 alphabetic codes.

Codes are unique and match the shapes ``JobPreferences.cv_country`` and ``Salary.currency``
accept; names are plain, unique, and the tuples are sorted by name, ignoring case and accents
(so "Åland Islands" sorts with the "A"s).
"""

import re
import unicodedata

import pytest

from app.domain.locales import COUNTRIES, CURRENCIES
from app.domain.settings import JobPreferences, Salary

CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def sort_key(name: str) -> str:
    """Case- and accent-insensitive."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


@pytest.mark.parametrize(
    ("table", "pattern"),
    [
        pytest.param(COUNTRIES, r"^[A-Z]{2}$", id="countries"),
        pytest.param(CURRENCIES, r"^[A-Z]{3}$", id="currencies"),
    ],
)
def test_locale_tables_are_code_name_pairs_unique_and_sorted_by_name(
    table: tuple[tuple[str, str], ...], pattern: str
) -> None:
    assert isinstance(table, tuple)
    assert table, "not empty"
    for entry in table:
        assert isinstance(entry, tuple) and len(entry) == 2, entry
        code, name = entry
        assert re.fullmatch(pattern, code), code
        assert isinstance(name, str) and name == name.strip() and name, entry
        assert 1 <= len(name) <= 100, entry
        assert not CONTROL.search(name), entry
    codes = [code for code, _ in table]
    names = [name for _, name in table]
    assert len(set(codes)) == len(codes), "codes are unique"
    assert len(set(names)) == len(names), "names are unique"
    assert names == sorted(names, key=sort_key), "sorted by name"


def test_countries_cover_the_assigned_iso_3166_list() -> None:
    by_code = dict(COUNTRIES)
    assert len(COUNTRIES) >= 240, "the full assigned ISO 3166-1 alpha-2 list"
    assert by_code["PT"] == "Portugal"
    assert by_code["DE"] == "Germany"
    assert "United Kingdom" in by_code["GB"]
    assert "United States" in by_code["US"]
    for unassigned in ("ZZ", "AA", "QM", "XX"):
        assert unassigned not in by_code, unassigned
    # "UK" is not the ISO code for the United Kingdom.
    assert "UK" not in by_code


def test_currencies_include_common_codes() -> None:
    by_code = dict(CURRENCIES)
    for code in ("EUR", "GBP", "USD", "CHF", "BRL"):
        assert code in by_code, code
    assert "Euro" in by_code["EUR"]
    assert "Pound" in by_code["GBP"] or "pound" in by_code["GBP"]
    assert "Dollar" in by_code["USD"] or "dollar" in by_code["USD"]
    assert "ZZZ" not in by_code


def test_every_listed_code_is_accepted_by_the_settings_models() -> None:
    for code, _ in COUNTRIES:
        JobPreferences(target_roles=("Fictional Archivist",), cv_country=code)
    for code, _ in CURRENCIES:
        Salary(amount=1, currency=code)
