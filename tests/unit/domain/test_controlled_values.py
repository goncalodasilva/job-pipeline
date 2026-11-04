"""M9.2 fix pass: the rules for the values and other spellings the Lists editor saves
(``app/domain/controlled_values.py``; ADR-131 items 6-7; spec 2.9 rule 3). Pure; every value is
fictional.

* ``clean_aliases`` keeps the spellings the user typed. It drops a repeat only when the
  lower-cased text is exactly the same and a spelling that is textually the value itself; two
  spellings that differ in punctuation or spacing ("full-time", "full time") are both kept,
  because the seeded lists hold them. A spelling whose ``value_key`` belongs to another value or
  spelling of the field is still refused.
* At most ``MAX_ALIASES`` (50) lines per value or skill, and no control character in a value or
  a spelling, each with fixed copy that never holds the typed text.
"""

import pytest

from app.domain.controlled_values import (
    MAX_ALIASES,
    NO_CONTROL,
    TOO_MANY,
    clean_aliases,
    clean_value,
    skill_alias_lines,
)
from app.domain.errors import ControlledValueInvalid
from app.domain.job_normalization import value_key

# Built with chr() so no invisible character sits in the source: a bell, a tab, an escape, a
# bidi override (U+202E), a line separator (U+2028) and a C1 control (U+0085).
BAD_TEXTS = [
    "a\x07b",
    "a\tb",
    "a\x1bb",
    "a" + chr(0x202E) + "b",
    "a" + chr(0x2028) + "b",
    "a" + chr(0x85) + "b",
]


def test_clean_aliases_keeps_spellings_that_differ_only_in_punctuation_or_spacing() -> None:
    """The seeded ``full_time`` has ["full-time", "full time"]; saving them again keeps both."""
    assert clean_aliases("full-time\nfull time", "full_time", set()) == ["full-time", "full time"]
    assert clean_aliases("Full-Time\r\nFULL TIME\r\n", "full_time", set()) == [
        "full-time",
        "full time",
    ]


def test_clean_aliases_drops_exact_repeats_and_the_values_own_text_only() -> None:
    assert clean_aliases("jr\nJR\n jr \nsr", "junior", set()) == ["jr", "sr"]
    assert clean_aliases("real_estate\nproperty", "real_estate", set()) == ["property"]
    # Same key as the value but not the same text: it is a spelling the user typed, so it stays.
    assert value_key("real estate") == value_key("real_estate")
    assert clean_aliases("real estate", "real_estate", set()) == ["real estate"]


def test_clean_aliases_keeps_the_order_typed() -> None:
    assert clean_aliases("b\na\nc", "x", set()) == ["b", "a", "c"]


@pytest.mark.parametrize("typed", ["energy", "E N E R G Y", "Ener-gy", "ok\nenergy"])
def test_clean_aliases_still_refuses_what_belongs_to_another_value(typed: str) -> None:
    with pytest.raises(ControlledValueInvalid) as error:
        clean_aliases(typed, "agriculture", {value_key("energy"), value_key("utilities")})
    assert set(error.value.fields) == {"aliases"}
    assert "energy" not in error.value.fields["aliases"].lower()


def test_clean_aliases_allows_exactly_the_maximum_and_refuses_one_more() -> None:
    lines = [f"spelling {n}" for n in range(MAX_ALIASES)]
    assert len(clean_aliases("\n".join(lines), "x", set())) == MAX_ALIASES
    with pytest.raises(ControlledValueInvalid) as error:
        clean_aliases("\n".join([*lines, "one more"]), "x", set())
    assert error.value.fields == {"aliases": TOO_MANY}
    assert TOO_MANY == "Too many other spellings. Keep up to 50."


def test_the_cap_counts_lines_not_what_is_left_after_repeats() -> None:
    """Fifty-one lines are refused even when repeats would leave fewer: the box is the limit."""
    typed = "\n".join(["same"] * (MAX_ALIASES + 1))
    with pytest.raises(ControlledValueInvalid) as error:
        clean_aliases(typed, "x", set())
    assert error.value.fields == {"aliases": TOO_MANY}


@pytest.mark.parametrize("bad", BAD_TEXTS)
def test_clean_aliases_refuses_control_characters_with_fixed_copy(bad: str) -> None:
    with pytest.raises(ControlledValueInvalid) as error:
        clean_aliases(f"fine\n{bad}", "x", set())
    assert error.value.fields == {"aliases": NO_CONTROL}


@pytest.mark.parametrize("bad", BAD_TEXTS[:2] + BAD_TEXTS[3:4])
def test_clean_value_refuses_control_characters_with_fixed_copy(bad: str) -> None:
    with pytest.raises(ControlledValueInvalid) as error:
        clean_value(bad, set())
    assert error.value.fields == {"value": NO_CONTROL}


def test_skill_alias_lines_cap_and_control_characters() -> None:
    lines = [f"alias {n}" for n in range(MAX_ALIASES)]
    assert skill_alias_lines("\n".join(lines)) == lines
    with pytest.raises(ControlledValueInvalid) as many:
        skill_alias_lines("\n".join([*lines, "one more"]))
    assert many.value.fields == {"aliases": TOO_MANY}
    with pytest.raises(ControlledValueInvalid) as control:
        skill_alias_lines("fine\nbad\x07line")
    assert control.value.fields == {"aliases": NO_CONTROL}
