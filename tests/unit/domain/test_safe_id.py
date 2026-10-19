"""``is_safe_id`` (M3.6 review): the one shape an id may have before it is looked up, put in a
URL or used as an element id: 1 to 64 of ``A-Z a-z 0-9 _ -``, what every ``IdFactory`` makes.
Facts and imports use it; tasks keep ``is_task_id``, the same shape. Every value is fictional."""

import pytest

from app.domain.ids import UuidIdFactory
from tests.fakes.ids import SequentialIdFactory

SAFE = [
    "seed-0001",
    "imp-a",
    "a",
    "A_b-9",
    "x" * 64,
    "3f2b8c1e-7d4a-4c59-9e0b-5a6d7c8e9f10",
    SequentialIdFactory("seed").new(),
    UuidIdFactory().new(),
]
UNSAFE = [
    None,
    7,
    b"seed-0001",
    "",
    " ",
    "x" * 65,
    "../seed-0001",
    "seed-0001/edit",
    "..",
    "seed.0001",
    "seed-0001?x=1",
    "seed-0001#x",
    "seed 0001",
    "seed-0001\n",
    "seed-0001\x00",
    "seed-0001%2F",
    "<b>seed</b>",
    "tâche-0001",
    "seed-\u0660\u0660\u0660\u0661",  # Arabic-Indic digits
]


@pytest.mark.parametrize("value", SAFE)
def test_is_safe_id_accepts_factory_ids(value: str) -> None:
    from app.domain.ids import is_safe_id

    assert is_safe_id(value) is True


@pytest.mark.parametrize("value", UNSAFE)
def test_is_safe_id_refuses_everything_else(value: object) -> None:
    from app.domain.ids import is_safe_id

    assert is_safe_id(value) is False


@pytest.mark.parametrize("value", [*SAFE, *UNSAFE])
def test_is_safe_id_is_the_shape_of_a_task_id(value: object) -> None:
    from app.domain.ids import is_safe_id
    from app.domain.tasks import is_task_id

    assert is_safe_id(value) is is_task_id(value)
