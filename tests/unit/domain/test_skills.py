"""Skill name normalization and alias matching (M3.3; ADR-089 item 11, ADR-090 item 9).

``app.domain.skills`` is pure:

- ``normalize_skill(name) -> str``: what ``skills.normalized_name`` stores. Unicode compatibility
  forms are folded (NFKC), case is folded, runs of whitespace become one space and the ends are
  trimmed. Punctuation counts, so "C++", "C#" and "C" stay three skills. A blank name gives "".
- ``skill_matches(candidate, name, aliases) -> bool``: whether ``candidate`` means the skill
  called ``name`` with those other spellings, compared after normalization.

Imports are inside the tests so each one fails on its own while the module is missing.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

NBSP = chr(0xA0)
SHARP_S = chr(0xDF)
# "SQL" in full-width letters, which NFKC folds to ASCII.
FULL_WIDTH_SQL = chr(0xFF33) + chr(0xFF31) + chr(0xFF2C)

SAME_SKILL = [
    pytest.param("Forecasting", "FORECASTING", id="case"),
    pytest.param("Forecasting", "  forecasting\t", id="surrounding-space"),
    pytest.param("Data analysis", "data   Analysis", id="inner-spaces"),
    pytest.param("Data analysis", "Data" + NBSP + "analysis", id="no-break-space"),
    pytest.param("Data analysis", "Data\nanalysis", id="line-break"),
    pytest.param("SQL", FULL_WIDTH_SQL, id="full-width-letters"),
    pytest.param("Stra" + SHARP_S + "enbau", "STRASSENBAU", id="case-folding-not-lowercasing"),
]

DIFFERENT_SKILLS = [
    pytest.param("C", "C++", id="plus-signs-count"),
    pytest.param("C", "C#", id="hash-counts"),
    pytest.param("Forecasting", "Forecast", id="no-stemming"),
    pytest.param("Data analysis", "Dataanalysis", id="a-space-is-not-nothing"),
]


@pytest.mark.parametrize(("first", "second"), SAME_SKILL)
def test_spellings_of_one_skill_normalize_alike(first: str, second: str) -> None:
    from app.domain.skills import normalize_skill

    assert normalize_skill(first) == normalize_skill(second)


@pytest.mark.parametrize(("first", "second"), DIFFERENT_SKILLS)
def test_different_skills_stay_different(first: str, second: str) -> None:
    from app.domain.skills import normalize_skill

    assert normalize_skill(first) != normalize_skill(second)


def test_normalized_name_is_trimmed_single_spaced_text() -> None:
    from app.domain.skills import normalize_skill

    assert normalize_skill("  Stakeholder \t Reporting ") == "stakeholder reporting"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n", NBSP])
def test_blank_name_normalizes_to_empty(blank: str) -> None:
    from app.domain.skills import normalize_skill

    assert normalize_skill(blank) == ""


@given(st.text())
def test_skill_normalization_idempotent(name: str) -> None:
    from app.domain.skills import normalize_skill

    once = normalize_skill(name)
    assert normalize_skill(once) == once
    # What is stored never has edge or doubled whitespace, whatever came in.
    assert once == " ".join(once.split())


@given(st.text())
def test_normalization_ignores_case_of_ascii_names(name: str) -> None:
    from app.domain.skills import normalize_skill

    assert normalize_skill(name.upper()) == normalize_skill(name.lower()) or not name.isascii()


def test_alias_matches_canonical_skill() -> None:
    from app.domain.skills import skill_matches

    aliases = ("Demand planning", "S&OP")
    assert skill_matches("Forecasting", "Forecasting", aliases)
    assert skill_matches("forecasting ", "Forecasting", aliases)
    assert skill_matches("demand  PLANNING", "Forecasting", aliases)
    assert skill_matches("s&op", "Forecasting", aliases)
    assert not skill_matches("Planning", "Forecasting", aliases)
    assert not skill_matches("", "Forecasting", aliases)
    assert not skill_matches("Demand planning", "Forecasting", ())


@given(st.text(), st.lists(st.text(), max_size=4))
def test_every_alias_and_the_name_match_unless_blank(name: str, aliases: list[str]) -> None:
    from app.domain.skills import normalize_skill, skill_matches

    for spelling in [name, *aliases]:
        expected = normalize_skill(spelling) != ""
        assert skill_matches(spelling, name, tuple(aliases)) is expected


# --- invisible characters --------------------------------------------------------

ZERO_WIDTH_SPACE = chr(0x200B)
ZERO_WIDTH_JOINER = chr(0x200D)
SOFT_HYPHEN = chr(0xAD)
BYTE_ORDER_MARK = chr(0xFEFF)
NUL = chr(0)
ESCAPE = chr(0x1B)
LONE_SURROGATE = chr(0xD800)

INVISIBLE = [
    pytest.param(ZERO_WIDTH_SPACE, id="zero-width-space"),
    pytest.param(ZERO_WIDTH_JOINER, id="zero-width-joiner"),
    pytest.param(SOFT_HYPHEN, id="soft-hyphen"),
    pytest.param(BYTE_ORDER_MARK, id="byte-order-mark"),
    pytest.param(NUL, id="nul"),
    pytest.param(ESCAPE, id="escape"),
    pytest.param(LONE_SURROGATE, id="lone-surrogate"),
]


@pytest.mark.parametrize("invisible", INVISIBLE)
def test_invisible_characters_do_not_make_another_skill(invisible: str) -> None:
    from app.domain.skills import normalize_skill, skill_matches

    assert normalize_skill("Python" + invisible) == normalize_skill("Python")
    assert normalize_skill(invisible + "Py" + invisible + "thon") == "python"
    assert normalize_skill("Data " + invisible + " analysis") == "data analysis"
    assert skill_matches("Python" + invisible, "Python", ())
    assert skill_matches("s&op", "Forecasting", ("S&" + invisible + "OP",))


@pytest.mark.parametrize("invisible", INVISIBLE)
def test_name_of_invisible_characters_only_is_blank(invisible: str) -> None:
    from app.domain.skills import display_skill, normalize_skill, skill_matches

    for name in (invisible, invisible * 3, f" {invisible}\t{invisible} "):
        assert normalize_skill(name) == ""
        assert display_skill(name) == ""
        assert not skill_matches(name, "Forecasting", (name,))


@pytest.mark.parametrize("invisible", INVISIBLE)
def test_shown_name_drops_invisible_characters_and_keeps_the_spelling(invisible: str) -> None:
    from app.domain.skills import display_skill

    assert display_skill("Python" + invisible) == "Python"
    assert display_skill(f"  Demand{invisible}  Fore{invisible}casting ") == "Demand Forecasting"
    assert display_skill("Data " + invisible + " analysis") == "Data analysis"


def test_shown_name_keeps_case_punctuation_and_letters_as_typed() -> None:
    from app.domain.skills import display_skill

    assert display_skill("  C++ ") == "C++"
    assert display_skill("Stra" + SHARP_S + "enbau\tPlanung") == "Stra" + SHARP_S + "enbau Planung"
    assert display_skill(FULL_WIDTH_SQL) == FULL_WIDTH_SQL


@given(st.text())
def test_shown_name_means_the_same_skill_and_is_stable(name: str) -> None:
    from app.domain.skills import display_skill, normalize_skill

    shown = display_skill(name)
    assert display_skill(shown) == shown
    assert shown == " ".join(shown.split())
    assert normalize_skill(shown) == normalize_skill(name)


@given(st.text())
def test_normalized_name_has_no_invisible_characters(name: str) -> None:
    import unicodedata

    from app.domain.skills import normalize_skill

    assert not any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in normalize_skill(name)
    )
