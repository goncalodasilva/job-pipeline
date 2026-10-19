"""Word bags made once per text (M3.7 review): comparing them gives exactly ``similarity``,
so a list can split each wording once and compare it with many. Pure; fictional text."""

from hypothesis import given
from hypothesis import strategies as st

from app.domain.similarity import similarity, words, words_similarity


@given(st.text(max_size=80), st.text(max_size=80))
def test_words_similarity_is_similarity(a: str, b: str) -> None:
    assert words_similarity(words(a), words(b)) == similarity(a, b)


def test_words_fold_case_and_drop_punctuation() -> None:
    assert words("Planned 60 ROUTES, planned!") == {"planned": 2, "60": 1, "routes": 1}
