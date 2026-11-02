"""Pure debrief rules (M8.3 part A; AC-M8.5; ADR-124 item 9; design "Debrief").

Assumed interface (chosen here where the sources left it open; the implementer matches it).
``app/domain/debrief.py`` is pure Python (no I/O, framework, clock or ids):

- ``DEBRIEF_WINDOW_DAYS == 14``: Today offers a debrief for an interview until this many days
  after its end.
- ``suggest_status(next_step, current_key, current_phase) -> str | None``. ``next_step`` is
  what the person says happens next: ``"next_round"``, ``"offer"``, ``"rejected"``, ``"other"``
  or nothing. The answer is the key of the status to propose, or None. The current status
  needs its phase as well as its key because a custom status can be in any phase (the built-in
  keys are ``preparing``, ``applied``, ``screening``, ``interviewing``, ``offer`` and the closed
  ``rejected``, ``withdrawn``, ``accepted``, ``declined``). A status is proposed ONLY when it
  would be a real change the person has to approve:

  ============  ===========================================================
  next step     proposed status, and when there is none
  ============  ===========================================================
  next_round    ``interviewing``; none when already ``interviewing``, ``offer`` or closed
  offer         ``offer``; none when already ``offer`` or closed
  rejected      ``rejected``; none when already closed (any closed status)
  other, ...    none (also for ``None``, a blank or an unknown word)
  ============  ===========================================================

  The function is total: it never raises and never returns the current key.
- ``parse_asked_questions(text) -> tuple[str, ...]``: the questions asked, one per line. Line
  ends are LF, CRLF or CR; each line is trimmed; blank lines are dropped; a line repeated in the
  same submission is kept once (the first, in order). ``ValueError`` (a message that never
  holds the text itself) for more than 20 questions, a question over 1,000 characters, a hidden
  or bidirectional character (control characters, the line and paragraph separators, the
  bidirectional marks and isolates, as ``app.domain.question_bank`` refuses them), or a text
  over the 4,000 characters a debrief keeps.
- ``split_experiences(text) -> tuple[str, ...]``: the experiences as paragraphs, split on blank
  lines (a line of only spaces is blank), each trimmed, none empty; a single newline stays
  inside its paragraph. ``ValueError`` (a message that never holds the text) for more than
  ``MAX_EXPERIENCE_PARAGRAPHS`` (3) paragraphs, a hidden or bidirectional character (a newline
  is allowed) or a text over 4,000 characters.

Everything is fictional.
"""

import ast
import importlib
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st

DOMAIN = Path(__file__).resolve().parents[3] / "app" / "domain"
BUILT_IN = {
    "preparing": "preparing",
    "applied": "active",
    "screening": "active",
    "interviewing": "active",
    "offer": "active",
    "rejected": "closed",
    "withdrawn": "closed",
    "accepted": "closed",
    "declined": "closed",
}
CLOSED = [key for key, phase in BUILT_IN.items() if phase == "closed"]
STEPS = ["next_round", "offer", "rejected", "other"]
MARKER = "FictionalDebriefMarkerLarkspur"


def module() -> ModuleType:
    return importlib.import_module("app.domain.debrief")


def suggest(step: str | None, key: str, phase: str) -> str | None:
    found: str | None = module().suggest_status(step, key, phase)
    return found


def test_the_window_is_fourteen_days() -> None:
    assert module().DEBRIEF_WINDOW_DAYS == 14


# --- suggest_status: the whole table ---------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("preparing", "interviewing"),
        ("applied", "interviewing"),
        ("screening", "interviewing"),
        ("interviewing", None),
        ("offer", None),
        ("rejected", None),
        ("withdrawn", None),
        ("accepted", None),
        ("declined", None),
    ],
)
def test_ac_m8_5_next_round_proposes_interviewing_only_as_a_real_change(
    key: str, expected: str | None
) -> None:
    assert suggest("next_round", key, BUILT_IN[key]) == expected


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("preparing", "offer"),
        ("applied", "offer"),
        ("screening", "offer"),
        ("interviewing", "offer"),
        ("offer", None),
        ("rejected", None),
        ("withdrawn", None),
        ("accepted", None),
        ("declined", None),
    ],
)
def test_ac_m8_5_offer_proposes_offer_only_as_a_real_change(key: str, expected: str | None) -> None:
    assert suggest("offer", key, BUILT_IN[key]) == expected


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("preparing", "rejected"),
        ("applied", "rejected"),
        ("screening", "rejected"),
        ("interviewing", "rejected"),
        ("offer", "rejected"),
        ("rejected", None),
        ("withdrawn", None),
        ("accepted", None),
        ("declined", None),
    ],
)
def test_ac_m8_5_rejected_proposes_rejected_unless_already_closed(
    key: str, expected: str | None
) -> None:
    assert suggest("rejected", key, BUILT_IN[key]) == expected


@pytest.mark.parametrize("key", list(BUILT_IN))
@pytest.mark.parametrize("step", ["other", None, "", "  ", "unknown", "Next_Round", "OFFER"])
def test_any_other_step_proposes_nothing(step: str | None, key: str) -> None:
    assert suggest(step, key, BUILT_IN[key]) is None


@pytest.mark.parametrize(
    ("step", "key", "phase", "expected"),
    [
        # A custom status is judged by its phase; only the built-in keys are special by key.
        ("next_round", "on_site", "active", "interviewing"),
        ("next_round", "wishlist", "preparing", "interviewing"),
        ("next_round", "paused", "closed", None),
        ("offer", "on_site", "active", "offer"),
        ("offer", "paused", "closed", None),
        ("rejected", "on_site", "active", "rejected"),
        ("rejected", "wishlist", "preparing", "rejected"),
        ("rejected", "paused", "closed", None),
    ],
)
def test_custom_statuses_follow_their_phase(
    step: str, key: str, phase: str, expected: str | None
) -> None:
    assert suggest(step, key, phase) == expected


CURRENT = st.one_of(
    st.sampled_from(list(BUILT_IN.items())),
    st.tuples(
        st.text(alphabet="abcdefgh_", min_size=1, max_size=12).map(lambda k: "custom_" + k),
        st.sampled_from(["preparing", "active", "closed"]),
    ),
)


@given(step=st.one_of(st.none(), st.text(max_size=20), st.sampled_from(STEPS)), current=CURRENT)
def test_suggest_status_is_total_and_never_proposes_the_current_status(
    step: str | None, current: tuple[str, str]
) -> None:
    key, phase = current
    found = suggest(step, key, phase)
    assert found in {None, "interviewing", "offer", "rejected"}
    assert found != key


# --- parse_asked_questions -------------------------------------------------------------------


def parse(text: str) -> tuple[str, ...]:
    found: tuple[str, ...] = module().parse_asked_questions(text)
    return found


def test_questions_are_one_per_line_trimmed_in_order() -> None:
    text = "  Walk me through a route you planned.  \nHow do you handle a late delivery?\n"
    assert parse(text) == (
        "Walk me through a route you planned.",
        "How do you handle a late delivery?",
    )


@pytest.mark.parametrize("ends", ["\n", "\r\n", "\r"])
def test_all_line_ends_split_and_blank_lines_are_dropped(ends: str) -> None:
    text = ends.join(["First question?", "", "   ", "Second question?", ""])
    assert parse(text) == ("First question?", "Second question?")


@pytest.mark.parametrize("text", ["", "   ", "\n\n", " \r\n \r\n"])
def test_no_questions_is_an_empty_tuple(text: str) -> None:
    assert parse(text) == ()


def test_a_line_repeated_in_one_submission_is_kept_once_in_first_place() -> None:
    text = "Why this role?\nWalk me through a route.\nWhy this role?\n  Why this role?  "
    assert parse(text) == ("Why this role?", "Walk me through a route.")


def test_twenty_questions_are_allowed_and_twenty_one_are_not() -> None:
    twenty = [f"Fictional question number {chr(65 + i)} about routes?" for i in range(20)]
    assert parse("\n".join(twenty)) == tuple(twenty)
    with pytest.raises(ValueError):
        parse("\n".join([*twenty, "One more fictional question about forklifts?"]))


def test_repeated_lines_do_not_count_against_the_twenty() -> None:
    distinct = [f"Fictional question number {chr(65 + i)} about routes?" for i in range(20)]
    assert len(parse("\n".join([*distinct, distinct[0], distinct[1]]))) == 20


def test_a_question_of_1000_characters_is_allowed_and_1001_is_not() -> None:
    exactly = "Q" + "a" * 999
    assert len(exactly) == 1000 and parse(exactly) == (exactly,)
    with pytest.raises(ValueError) as refused:
        parse(MARKER + "a" * (1001 - len(MARKER)))
    assert MARKER not in str(refused.value), "the message never holds the text"


def test_questions_over_the_4000_characters_a_debrief_keeps_are_refused() -> None:
    fits = "\n".join(f"{i}" + "x" * 899 for i in range(4))  # 4 x 900 + 3 line ends
    assert len(parse(fits)) == 4
    with pytest.raises(ValueError):
        parse("\n".join(f"{i}" + "x" * 899 for i in range(5)))  # 5 x 900 + 4 line ends


HIDDEN_CODES = [
    0x00, 0x07, 0x1B, 0x7F, 0x85, 0x061C, 0x200E, 0x200F, 0x2028, 0x2029,
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069,
]  # fmt: skip


@pytest.mark.parametrize("code", HIDDEN_CODES, ids=lambda c: f"U+{c:04X}")
def test_hidden_and_bidirectional_characters_are_refused(code: int) -> None:
    char = chr(code)
    text = f"A fine question?\nHow did you plan {MARKER}{char}routes?"
    with pytest.raises(ValueError) as refused:
        parse(text)
    assert MARKER not in str(refused.value) and "fine question" not in str(refused.value)


def test_markup_is_kept_as_plain_text() -> None:
    hostile = '<script>alert(1)</script> & "quotes"?'
    assert parse(hostile) == (hostile,)


# --- split_experiences -----------------------------------------------------------------------


def split(text: str) -> tuple[str, ...]:
    found: tuple[str, ...] = module().split_experiences(text)
    return found


@pytest.mark.parametrize("text", ["", "   ", "\n\n\n", " \n  \n "])
def test_no_experiences_is_an_empty_tuple(text: str) -> None:
    assert split(text) == ()


def test_one_paragraph_keeps_its_single_newlines() -> None:
    assert split("  I planned 12 routes.\nThen I checked them.  ") == (
        "I planned 12 routes.\nThen I checked them.",
    )


@pytest.mark.parametrize(
    "text",
    [
        "First paragraph.\n\nSecond paragraph.",
        "First paragraph.\n\n\n\nSecond paragraph.",
        "First paragraph.\n   \nSecond paragraph.",
        "First paragraph.\r\n\r\nSecond paragraph.",
        "\n\nFirst paragraph.\n\nSecond paragraph.\n\n",
    ],
    ids=["blank", "many-blank", "spaces-only-line", "crlf", "outer-blank"],
)
def test_blank_lines_split_paragraphs(text: str) -> None:
    assert split(text) == ("First paragraph.", "Second paragraph.")


def test_three_paragraphs_are_allowed_and_four_are_not() -> None:
    three = "One.\n\nTwo.\n\nThree."
    assert split(three) == ("One.", "Two.", "Three.")
    with pytest.raises(ValueError):
        split(three + "\n\nFour.")


def test_experiences_over_4000_characters_are_refused_and_4000_are_not() -> None:
    assert len(split("x" * 4000)) == 1
    with pytest.raises(ValueError) as refused:
        split(MARKER + "x" * (4001 - len(MARKER)))
    assert MARKER not in str(refused.value)


@pytest.mark.parametrize("code", [0x00, 0x07, 0x1B, 0x7F, 0x202E, 0x2066, 0x200F], ids=hex)
def test_experiences_refuse_hidden_and_bidirectional_characters(code: int) -> None:
    char = chr(code)
    with pytest.raises(ValueError) as refused:
        split(f"I led {MARKER}{char}the team.\n\nSecond paragraph.")
    assert MARKER not in str(refused.value)


# --- purity ----------------------------------------------------------------------------------


def test_debrief_module_is_pure_python() -> None:
    path = DOMAIN / "debrief.py"
    assert path.is_file(), "app/domain/debrief.py is missing"
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
    forbidden = {
        "os",
        "pathlib",
        "datetime",
        "time",
        "uuid",
        "random",
        "sqlite3",
        "fastapi",
        "starlette",
        "httpx",
        "jinja2",
    }
    assert not {i for i in imported if i.split(".")[0] in forbidden}, imported
    assert not {i for i in imported if i.startswith(("app.modules", "app.services", "app.api"))}


# --- review fixes: format characters, tabs and "how it went" (M8.3 part A review) -------------

FORMAT_CODES = [0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0xE0041]


def check_how(text: str) -> str:
    found: str = module().check_how_it_went(text)
    return found


@pytest.mark.parametrize("code", FORMAT_CODES, ids=lambda c: f"U+{c:04X}")
def test_format_characters_are_refused_in_questions_and_experiences(code: int) -> None:
    char = chr(code)
    with pytest.raises(ValueError) as in_questions:
        parse(f"A fine question?\nHow did {MARKER}{char}you plan?")
    assert MARKER not in str(in_questions.value)
    with pytest.raises(ValueError) as in_experiences:
        split(f"I led {MARKER}{char}the team.")
    assert MARKER not in str(in_experiences.value)


@pytest.mark.parametrize("code", FORMAT_CODES, ids=lambda c: f"U+{c:04X}")
def test_format_characters_are_refused_in_how_it_went(code: int) -> None:
    with pytest.raises(ValueError) as refused:
        check_how(f"It went {MARKER}{chr(code)}well.")
    assert MARKER not in str(refused.value)


@pytest.mark.parametrize("code", HIDDEN_CODES, ids=lambda c: f"U+{c:04X}")
def test_hidden_and_bidirectional_characters_are_refused_in_how_it_went(code: int) -> None:
    with pytest.raises(ValueError) as refused:
        check_how(f"It went {MARKER}{chr(code)}well.")
    assert MARKER not in str(refused.value)


def test_how_it_went_keeps_newlines_and_is_not_trimmed() -> None:
    assert check_how("  Calm.\nFriendly.\n\nGood.  \n") == "  Calm.\nFriendly.\n\nGood.  \n"
    assert check_how("") == ""


def test_how_it_went_over_4000_characters_is_refused_and_4000_are_not() -> None:
    assert len(check_how("x" * 4000)) == 4000
    with pytest.raises(ValueError) as refused:
        check_how(MARKER + "x" * (4001 - len(MARKER)))
    assert MARKER not in str(refused.value)


def test_a_tab_is_a_space_in_every_text() -> None:
    assert check_how("Calm.\tFriendly.") == "Calm. Friendly."
    assert parse("Why\tthis role?\n\tWalk me through it?") == (
        "Why this role?",
        "Walk me through it?",
    )
    assert split("I led\tthe team.\n\n\t\n\nSecond.") == ("I led the team.", "Second.")
