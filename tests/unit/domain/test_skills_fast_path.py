"""The ASCII fast path of ``normalize_skill`` gives what the slow fold-to-fixpoint path gives.

The slow path (``_fold`` repeated until nothing changes) is the definition; the fast path is only
a shortcut for plain printable ASCII (M7.10). This oracle compares the two on generated text, on
the characters where a shortcut is most likely to differ (``C++``, ``C#``, dots, spaces, tabs) and
on pinned examples.
"""

import string

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.skills import _fold, normalize_skill

ALPHABET = string.printable + "C+#."


def slow(name: str) -> str:
    """The definition: fold until nothing changes."""
    folded = _fold(name)
    while (again := _fold(folded)) != folded:
        folded = again
    return folded


@settings(max_examples=500, deadline=None)
@given(st.text(alphabet=ALPHABET))
def test_normalize_skill_fast_path_matches_the_fold_to_fixpoint(name: str) -> None:
    assert normalize_skill(name) == slow(name)


@settings(max_examples=500, deadline=None)
@given(
    st.lists(st.sampled_from([" ", " ", " ", " ", "C", "c", "+", "#", ".", "a", "Z", "9"])).map(
        "".join
    )
)
def test_normalize_skill_fast_path_matches_on_space_heavy_printable_text(name: str) -> None:
    # Only printable ASCII, so every example takes the fast path; runs and edges of spaces.
    assert name.isascii() and name.isprintable()
    assert normalize_skill(name) == slow(name)


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.characters(max_codepoint=0x2FF)))
def test_normalize_skill_matches_the_fold_beyond_ascii_too(name: str) -> None:
    assert normalize_skill(name) == slow(name)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("C++", "c++"),
        (" C# ", "c#"),
        ("", ""),
        ("\t", ""),
        ("  Apache   Spark ", "apache spark"),
        ("Node.JS", "node.js"),
        ("a\tb\nc", "a b c"),
    ],
)
def test_normalize_skill_pinned_examples(name: str, expected: str) -> None:
    assert normalize_skill(name) == expected == slow(name)
