"""The full-text query builder (M3.3; AC-M3.6; spec 3.3; migration 0003's note on ``facts_fts``).

``app.domain.fts.fts_query(text) -> str | None`` turns what the user typed into an FTS5 query
of quoted tokens: the text is split on whitespace, each piece is wrapped in double quotes with
any double quote inside doubled, and the pieces are joined with one space, so every piece must
be found and FTS5 operators (AND, OR, NOT, NEAR, ``*``, ``^``, ``column:``, brackets) are plain
text. Characters FTS5 can't take inside a string (NUL and other control characters) are left
out. Blank text gives ``None``: no text filter.

The property test runs the built query against an FTS5 table in memory (no file, no network).
Imports are inside the tests so each one fails on its own while the module is missing.
"""

import re
import sqlite3

import pytest
from hypothesis import given
from hypothesis import strategies as st

# One or more double-quoted strings separated by single spaces; inside, a quote is doubled.
QUOTED_TOKENS = re.compile(r'"(?:[^"]|"")*"(?: "(?:[^"]|"")*")*')

BUILDER_TABLE = [
    pytest.param("", None, id="empty"),
    pytest.param("   \t\n", None, id="blank"),
    pytest.param("budget", '"budget"', id="one-word"),
    pytest.param("budget forecast", '"budget" "forecast"', id="two-words"),
    pytest.param("  budget \t forecast\n", '"budget" "forecast"', id="any-whitespace-separates"),
    pytest.param('say "hi"', '"say" """hi"""', id="quotes-doubled"),
    pytest.param('"', '""""', id="lone-quote"),
    pytest.param("budget OR forecast", '"budget" "OR" "forecast"', id="or-is-a-word"),
    pytest.param("budget AND NOT plan", '"budget" "AND" "NOT" "plan"', id="and-not-are-words"),
    pytest.param("budg*", '"budg*"', id="star-is-text"),
    pytest.param("text:budget", '"text:budget"', id="column-filter-is-text"),
    pytest.param("NEAR(budget plan, 2)", '"NEAR(budget" "plan," "2)"', id="near-is-text"),
    pytest.param("^budget -plan", '"^budget" "-plan"', id="caret-and-minus-are-text"),
    pytest.param("(budget", '"(budget"', id="open-bracket-is-text"),
    pytest.param("month-end", '"month-end"', id="hyphenated-word-is-one-piece"),
]


@pytest.mark.parametrize(("typed", "expected"), BUILDER_TABLE)
def test_fts_query_builder(typed: str, expected: str | None) -> None:
    from app.domain.fts import fts_query

    assert fts_query(typed) == expected


def test_control_characters_never_reach_the_query() -> None:
    from app.domain.fts import fts_query

    built = fts_query("bud\x00get fore\x1fcast")
    assert built is not None
    assert "\x00" not in built
    assert "\x1f" not in built
    assert QUOTED_TOKENS.fullmatch(built)


def _index() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE VIRTUAL TABLE notes USING fts5 (fact_id UNINDEXED, text, role)")
    connection.execute(
        "INSERT INTO notes (fact_id, text, role) VALUES "
        "('f1', 'Cut month-end close from nine days to four', 'Lanternfish Logistics')"
    )
    return connection


@given(st.text())
def test_any_text_builds_quoted_tokens_that_fts5_accepts(typed: str) -> None:
    from app.domain.fts import fts_query

    built = fts_query(typed)
    if built is None:
        assert typed.strip() == "" or all(not piece.isprintable() for piece in typed.split())
        return
    assert QUOTED_TOKENS.fullmatch(built)
    connection = _index()
    try:
        # Raises sqlite3.OperationalError on a syntax error or an unterminated string.
        connection.execute("SELECT count(*) FROM notes WHERE notes MATCH ?", (built,)).fetchone()
    finally:
        connection.close()


@given(st.lists(st.sampled_from(["close", "four", "nine", "OR", "NOT", "AND", "zzzz"]), min_size=1))
def test_every_typed_word_must_be_found(words: list[str]) -> None:
    from app.domain.fts import fts_query

    built = fts_query(" ".join(words))
    assert built is not None
    connection = _index()
    try:
        found = connection.execute(
            "SELECT count(*) FROM notes WHERE notes MATCH ?", (built,)
        ).fetchone()[0]
    finally:
        connection.close()
    # The indexed text holds "close", "four" and "nine" and none of the other words.
    assert found == (1 if set(words) <= {"close", "four", "nine"} else 0)


# --- prefix queries: search as you type (the command palette, M9.5; ADR-138) -----------------

# Like QUOTED_TOKENS, with a star after each quoted piece.
PREFIX_TOKENS = re.compile(r'"(?:[^"]|"")*"\*(?: "(?:[^"]|"")*"\*)*')


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        pytest.param("", None, id="empty"),
        pytest.param("  \t", None, id="blank"),
        pytest.param("clo", '"clo"*', id="one-start"),
        pytest.param("clo fo", '"clo"* "fo"*', id="two-starts"),
        pytest.param('say "hi"', '"say"* """hi"""*', id="quotes-doubled"),
        pytest.param("budg*", '"budg*"*', id="star-is-text"),
        pytest.param("text:x OR", '"text:x"* "OR"*', id="operators-are-text"),
    ],
)
def test_fts_prefix_query_builder(typed: str, expected: str | None) -> None:
    from app.domain.fts import fts_prefix_query

    assert fts_prefix_query(typed) == expected


@given(st.text())
def test_any_text_builds_prefix_tokens_that_fts5_accepts(typed: str) -> None:
    from app.domain.fts import fts_prefix_query

    built = fts_prefix_query(typed)
    if built is None:
        assert typed.strip() == "" or all(not piece.isprintable() for piece in typed.split())
        return
    assert PREFIX_TOKENS.fullmatch(built)
    connection = _index()
    try:
        connection.execute("SELECT count(*) FROM notes WHERE notes MATCH ?", (built,)).fetchone()
    finally:
        connection.close()


@given(st.lists(st.sampled_from(["clo", "fou", "nin", "close", "lose", "zz", "OR"]), min_size=1))
def test_every_typed_start_must_be_found(words: list[str]) -> None:
    from app.domain.fts import fts_prefix_query

    built = fts_prefix_query(" ".join(words))
    assert built is not None
    connection = _index()
    try:
        found = connection.execute(
            "SELECT count(*) FROM notes WHERE notes MATCH ?", (built,)
        ).fetchone()[0]
    finally:
        connection.close()
    # Words of the text start with "clo", "fou", "nin" and "close"; "lose" is inside a word.
    assert found == (1 if set(words) <= {"clo", "fou", "nin", "close"} else 0)
