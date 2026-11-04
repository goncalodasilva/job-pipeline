"""The words and the line diff of Settings > Prompts (M9.3 fix pass 2; ADR-055; ADR-131 item 7).
Pure functions only: no app, no files. Every text is fictional."""

import pytest

from app.api.prompt_forms import CONTEXT_LINES, format_size, line_diff


def numbered(count: int) -> str:
    return "\n".join(f"line {n}" for n in range(1, count + 1))


def kinds(found: list[dict[str, str]]) -> list[str]:
    return [line["kind"] for line in found]


def test_identical_texts_have_no_lines_at_all() -> None:
    assert line_diff(numbered(8), numbered(8)) == []
    assert line_diff("a\r\nb", "a\nb") == []


def test_a_change_in_a_short_text_keeps_every_line_and_marks_the_changed_ones() -> None:
    found = line_diff("a\nb\nc", "a\nB\nc")

    assert kinds(found) == ["same", "removed", "added", "same"]
    assert [(line["marker"], line["text"]) for line in found] == [
        (" ", "a"),
        ("-", "b"),
        ("+", "B"),
        (" ", "c"),
    ]
    assert [line["word"] for line in found] == ["", "Removed: ", "Added: ", ""]


def test_a_long_unchanged_run_collapses_to_two_lines_of_context_and_a_count() -> None:
    assert CONTEXT_LINES == 2
    before = numbered(20)
    after = before.replace("line 10", "line ten")

    found = line_diff(before, after)

    assert kinds(found) == [
        "gap",  # lines 1 to 7 are out of reach
        "same",
        "same",
        "removed",
        "added",
        "same",
        "same",
        "gap",  # lines 13 to 20
    ]
    assert [line["text"] for line in found if line["kind"] == "gap"] == [
        "… 7 unchanged lines",
        "… 8 unchanged lines",
    ]
    assert [line["text"] for line in found if line["kind"] == "same"] == [
        "line 8",
        "line 9",
        "line 11",
        "line 12",
    ]


def test_a_gap_sits_between_two_changes_and_a_single_hidden_line_is_shown_instead() -> None:
    before = numbered(12)
    after = before.replace("line 1\n", "line one\n").replace("line 12", "line twelve")

    found = line_diff(before, after)

    assert [line["text"] for line in found if line["kind"] == "gap"] == ["… 6 unchanged lines"]
    # lines 2, 3 and 10, 11 are context; 4 to 9 are the gap
    one_hidden = line_diff(
        numbered(7), numbered(7).replace("line 1\n", "line one\n").replace("line 7", "line seven")
    )
    assert "gap" not in kinds(one_hidden), "one hidden line is simply shown"
    assert [line["text"] for line in one_hidden if line["kind"] == "same"] == [
        f"line {n}" for n in range(2, 7)
    ]


def test_every_gap_hides_at_least_two_lines_and_counts_them_exactly() -> None:
    before = numbered(30)
    after = before.replace("line 5\n", "line five\n").replace("line 25\n", "line twenty five\n")

    found = line_diff(before, after)

    shown_same = [line for line in found if line["kind"] == "same"]
    hidden = [int(line["text"].split()[1]) for line in found if line["kind"] == "gap"]
    assert all(count >= 2 for count in hidden)
    changed = 2  # one removed and one added line each, replacing one line each
    assert len(shown_same) + sum(hidden) + changed == 30


def test_a_line_that_looks_like_markup_stays_text() -> None:
    found = line_diff("a", '<script id="x">1</script>')

    assert [line["text"] for line in found] == ["a", '<script id="x">1</script>']


@pytest.mark.parametrize(
    ("size", "words"),
    [
        (0, "0 bytes"),
        (1, "1 byte"),
        (812, "812 bytes"),
        (1024, "1.0 KB"),
        (12_800, "12.5 KB"),
        (262_144, "256.0 KB"),
        (263_168, "257.0 KB"),
    ],
)
def test_sizes_are_plain_words(size: int, words: str) -> None:
    assert format_size(size) == words
