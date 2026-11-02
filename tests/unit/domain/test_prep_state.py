"""Pure prep state (M8.2 part D; ADR-124 item 10; design "Prep state").

Assumed interface (chosen here where the sources left it open; the implementer matches it).
``app/domain/prep_state.py`` (pure Python, no I/O, framework, clock or ids):

- ``prep_state(item_count, practiced_count) -> str`` is one of ``"not_started"`` (no prep
  items), ``"questions_ready"`` (items exist and none has a practice attempt) or ``"practiced"``
  (at least one item has an attempt). ``practiced_count`` counts items with at least one
  attempt, not attempts. Total over non-negative ints: it never raises, and more practice never
  moves the state back.
- ``PREP_STATE_LABELS``: the words shown for each state, exactly ``"Prep not started"``,
  ``"Questions ready"`` and ``"Practiced for the interview"`` (each names its subject, as it is
  shown beside "Next interview" on a board card and in Today).
- ``should_show(item_count, has_future_interview) -> bool``: the state is shown only when the
  application has prep items or a scheduled future interview, and never for a closed one
  (``closed=True``, an optional third argument that defaults to False).

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
STATES = ("not_started", "questions_ready", "practiced")
RANK = {state: rank for rank, state in enumerate(STATES)}
counts = st.integers(min_value=0, max_value=10_000)


def module() -> ModuleType:
    return importlib.import_module("app.domain.prep_state")


def test_the_three_states_and_their_words() -> None:
    labels = module().PREP_STATE_LABELS
    assert dict(labels) == {
        "not_started": "Prep not started",
        "questions_ready": "Questions ready",
        "practiced": "Practiced for the interview",
    }


@pytest.mark.parametrize(
    ("items", "practiced", "expected"),
    [
        (0, 0, "not_started"),
        (1, 0, "questions_ready"),
        (7, 0, "questions_ready"),
        (1, 1, "practiced"),
        (7, 1, "practiced"),
        (7, 7, "practiced"),
    ],
)
def test_prep_state_rules(items: int, practiced: int, expected: str) -> None:
    assert module().prep_state(items, practiced) == expected


@pytest.mark.parametrize(
    ("items", "future", "expected"),
    [
        (0, False, False),
        (0, True, True),
        (1, False, True),
        (1, True, True),
        (30, False, True),
    ],
)
def test_should_show_only_with_items_or_a_future_interview(
    items: int, future: bool, expected: bool
) -> None:
    assert module().should_show(items, future) is expected


@given(items=counts, practiced=counts)
def test_prep_state_is_total_and_returns_a_known_state(items: int, practiced: int) -> None:
    state = module().prep_state(items, practiced)
    assert state in STATES
    assert state in module().PREP_STATE_LABELS


@given(items=counts, practiced=counts, more=st.integers(min_value=0, max_value=1000))
def test_more_practice_never_moves_the_state_back(items: int, practiced: int, more: int) -> None:
    before = module().prep_state(items, practiced)
    after = module().prep_state(items, practiced + more)
    assert RANK[after] >= RANK[before]


@given(items=counts, practiced=counts, more=st.integers(min_value=0, max_value=1000))
def test_more_items_never_move_the_state_back(items: int, practiced: int, more: int) -> None:
    before = module().prep_state(items, practiced)
    after = module().prep_state(items + more, practiced)
    assert RANK[after] >= RANK[before]


@given(items=st.integers(min_value=1, max_value=10_000))
def test_a_practiced_item_is_always_practiced(items: int) -> None:
    assert module().prep_state(items, 1) == "practiced"
    assert module().prep_state(items, 0) == "questions_ready"


@pytest.mark.parametrize(("items", "future"), [(0, False), (0, True), (3, False), (3, True)])
def test_should_show_never_for_a_closed_application(items: int, future: bool) -> None:
    assert module().should_show(items, future, closed=True) is False
    assert module().should_show(items, future, closed=False) is (items > 0 or future)


@given(items=counts, future=st.booleans())
def test_should_show_is_total(items: int, future: bool) -> None:
    assert module().should_show(items, future) == (items > 0 or future)


def test_the_prep_state_module_stays_pure() -> None:
    path = DOMAIN / "prep_state.py"
    assert path.is_file(), "app/domain/prep_state.py is missing"
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
