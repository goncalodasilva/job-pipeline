"""Mechanical near-duplicate similarity (M3.7; ADR-089 item 7: no AI call).

``app/domain/similarity.py`` is pure Python:

- ``similarity(a, b) -> float`` in ``[0, 1]``, symmetric, insensitive to letter case,
  whitespace and punctuation; a text with at least one word is 1.0 alike to itself.
- ``SUGGEST_AT``: the threshold, strictly between 0 and 1.
- ``is_near_duplicate(a, b) -> bool``: ``similarity(a, b) >= SUGGEST_AT``; a blank text is
  never a duplicate.

The module is reached through ``importlib`` so a missing module fails each test with a plain
message rather than an error at collection. Every text is fictional.
"""

import importlib
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st

ROUTES = "Planned 60 delivery routes a week with a team of 3."
INVOICES = "Cut invoice processing time by 35% across 4 warehouses."

ASCII_TEXT = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,%-"),
    max_size=80,
)
ANY_TEXT = st.text(max_size=80)


def similarity_module() -> ModuleType:
    try:
        return importlib.import_module("app.domain.similarity")
    except ModuleNotFoundError:
        pytest.fail("app.domain.similarity doesn't exist yet")


def test_threshold_is_strictly_between_0_and_1() -> None:
    module = similarity_module()
    assert 0 < module.SUGGEST_AT < 1


@given(ANY_TEXT, ANY_TEXT)
def test_similarity_symmetric_and_bounded(a: str, b: str) -> None:
    module = similarity_module()
    forward = module.similarity(a, b)
    assert isinstance(forward, float)
    assert 0.0 <= forward <= 1.0
    assert forward == module.similarity(b, a), "the order of the two texts never matters"
    assert module.is_near_duplicate(a, b) == module.is_near_duplicate(b, a)


@given(ASCII_TEXT)
def test_text_with_a_word_is_fully_alike_to_itself_in_any_case(text: str) -> None:
    module = similarity_module()
    if not any(char.isalnum() for char in text):
        assert not module.is_near_duplicate(text, text), "a blank text is no duplicate"
        return
    assert module.similarity(text, text) == 1.0
    assert module.similarity(text, text.upper()) == 1.0
    assert module.similarity(text, text.lower()) == 1.0


@pytest.mark.parametrize(
    "other",
    [
        "PLANNED 60 delivery routes a week with a team of 3",
        "planned 60 delivery routes a week with a team of 3.",
        "  Planned 60   delivery routes\na week with a team of 3!  ",
    ],
    ids=["upper", "lower_no_stop", "spacing_and_punctuation"],
)
def test_same_text_other_case_is_duplicate(other: str) -> None:
    module = similarity_module()
    assert module.similarity(ROUTES, other) == 1.0
    assert module.is_near_duplicate(ROUTES, other)


def test_one_extra_word_is_still_a_near_duplicate() -> None:
    module = similarity_module()
    longer = "Cut the invoice processing time by 35% across 4 warehouses."
    assert module.is_near_duplicate(INVOICES, longer)
    assert module.similarity(INVOICES, longer) < 1.0


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (ROUTES, INVOICES),
        (
            "Led a team of 5 fictional engineers at Example Corp.",
            "Wrote the onboarding guide used by every new hire at Example Corp.",
        ),
        ("Fluent English", "Basic German"),
        ("Python", "Python and SQL for weekly reporting at Example Freight Lda"),
        ("", ROUTES),
        ("   ", "   "),
    ],
    ids=["unrelated", "same_employer", "two_words", "one_word_inside_longer", "blank", "spaces"],
)
def test_different_facts_not_suggested(a: str, b: str) -> None:
    module = similarity_module()
    assert not module.is_near_duplicate(a, b)
    if a.strip() and b.strip():
        assert module.similarity(a, b) < module.SUGGEST_AT
