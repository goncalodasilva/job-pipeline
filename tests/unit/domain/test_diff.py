"""Word-level diff for the fact history view (M3.2; groundwork for AC-M3.3).

``app.domain.diff.word_diff(old, new)`` is pure and built on ``difflib``. It returns segments,
each ``equal``, ``added`` or ``removed``, that keep every character of both texts: the equal
and added segments joined give the new text, the equal and removed ones the old text. Whole
words are marked, never parts of a word, so the history view can label changes by text as well
as by color (M3.6) and Refine with AI can show a suggestion as a diff (M3.9).

Imports are inside the tests so each one fails on its own while the module is missing. All
wording is fictional.
"""

import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st


def words(segments: object, op: str) -> list[str]:
    return [
        word
        for segment in segments  # type: ignore[attr-defined]
        if segment.op == op
        for word in segment.text.split()
    ]


def joined(segments: object, without: str) -> str:
    return "".join(segment.text for segment in segments if segment.op != without)  # type: ignore[attr-defined]


def test_diff_ops_are_equal_added_and_removed() -> None:
    from app.domain.diff import DiffOp

    assert {op.value for op in DiffOp} == {"equal", "added", "removed"}
    assert DiffOp("added") == "added"


def test_diff_marks_added_and_removed_words() -> None:
    from app.domain.diff import DiffOp, word_diff

    old = "Led a team of 5 engineers"
    new = "Led a team of 8 engineers and designers"
    segments = word_diff(old, new)

    assert words(segments, "removed") == ["5"]
    assert words(segments, "added") == ["8", "and", "designers"]
    assert words(segments, "equal") == ["Led", "a", "team", "of", "engineers"]
    assert joined(segments, without="removed") == new
    assert joined(segments, without="added") == old
    # Each segment is an immutable (op, text) value with some text in it.
    for segment in segments:
        assert isinstance(segment.op, DiffOp)
        assert segment.text != ""
        with pytest.raises(dataclasses.FrozenInstanceError):
            segment.text = "changed"
    assert isinstance(segments, tuple)


def test_diff_marks_whole_words_not_letters() -> None:
    from app.domain.diff import word_diff

    segments = word_diff("Improved the weekly forecast", "Improving the weekly forecasts")
    assert words(segments, "removed") == ["Improved", "forecast"]
    assert words(segments, "added") == ["Improving", "forecasts"]
    assert words(segments, "equal") == ["the", "weekly"]


def test_diff_of_identical_text_marks_nothing() -> None:
    from app.domain.diff import word_diff

    text = "Trained four new cashiers\nin two weeks"
    segments = word_diff(text, text)
    assert {segment.op.value for segment in segments} == {"equal"}
    assert joined(segments, without="removed") == text
    assert word_diff("", "") == ()


def test_diff_from_or_to_nothing_marks_everything() -> None:
    from app.domain.diff import word_diff

    added = word_diff("", "Rewrote the loading timetable")
    assert {segment.op.value for segment in added} == {"added"}
    assert words(added, "added") == ["Rewrote", "the", "loading", "timetable"]

    removed = word_diff("Rewrote the loading timetable", "")
    assert {segment.op.value for segment in removed} == {"removed"}
    assert joined(removed, without="added") == "Rewrote the loading timetable"
    assert joined(removed, without="removed") == ""


def test_diff_keeps_line_breaks_and_spacing() -> None:
    from app.domain.diff import word_diff

    old = "Situation:  the depot\nmissed its window\n"
    new = "Situation:  the depot\n\nnever missed its window again\n"
    segments = word_diff(old, new)
    assert joined(segments, without="removed") == new
    assert joined(segments, without="added") == old
    assert words(segments, "added") == ["never", "again"]
    assert words(segments, "removed") == []


def test_diff_treats_markup_as_plain_text() -> None:
    # Fact text is untrusted when it came from a CV; the diff returns it unchanged and never
    # produces markup of its own (templates escape it).
    from app.domain.diff import word_diff

    new = "<script>alert(1)</script> & <b>bold</b>"
    segments = word_diff("plain", new)
    assert joined(segments, without="removed") == new
    assert all("<ins" not in s.text and "<del" not in s.text for s in segments)


@given(old=st.text(max_size=200), new=st.text(max_size=200))
def test_diff_round_trips_to_new_text(old: str, new: str) -> None:
    from app.domain.diff import word_diff

    segments = word_diff(old, new)
    assert joined(segments, without="removed") == new
    assert joined(segments, without="added") == old
    assert all(segment.text != "" for segment in segments)


SENTENCE = st.lists(
    st.sampled_from(["led", "a", "team", "of", "5", "8", "engineers", "and", "the", "depot"]),
    max_size=12,
).map(" ".join)


@given(old=SENTENCE, new=SENTENCE)
def test_diff_round_trips_to_new_text_for_overlapping_sentences(old: str, new: str) -> None:
    # Random text rarely shares words; sentences from a small vocabulary do, so equal, added
    # and removed segments all occur. Marked segments never split a word.
    from app.domain.diff import word_diff

    segments = word_diff(old, new)
    assert joined(segments, without="removed") == new
    assert joined(segments, without="added") == old
    assert set(words(segments, "added")) <= set(new.split())
    assert set(words(segments, "removed")) <= set(old.split())
    assert words(segments, "equal") + words(segments, "added") != [] or new == ""
    if old == new:
        assert all(segment.op.value == "equal" for segment in segments)
