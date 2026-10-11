"""``is_task_id``: the one shape a task id may have before it reaches the database, a URL or
a template (M2.19 review): 1 to 64 of ``A-Z a-z 0-9 _ -``. Every value is fictional."""

import pytest

from app.domain.ids import UuidIdFactory
from tests.fakes.ids import SequentialIdFactory


@pytest.mark.parametrize(
    "value",
    [
        "t-0001",
        "task-0001",
        "id-0001",
        "a",
        "A_b-9",
        "x" * 64,
        "3f2b8c1e-7d4a-4c59-9e0b-5a6d7c8e9f10",
        SequentialIdFactory("t").new(),
        UuidIdFactory().new(),
    ],
)
def test_is_task_id_accepts_factory_ids(value: str) -> None:
    from app.domain.tasks import is_task_id

    assert is_task_id(value) is True


@pytest.mark.parametrize(
    "value",
    [
        None,
        7,
        b"t-0001",
        "",
        " ",
        "x" * 65,
        "../t-0001",
        "t-0001/..",
        "..",
        "t.0001",
        "t-0001?x=1",
        "t-0001#x",
        "t 0001",
        " t-0001",
        "t-0001 ",
        "t-0001\n",
        "t-0001\x00",
        "t:0001",
        "t-0001%2F",
        "<b>t</b>",
        "tâche-0001",
        "t-\u0660\u0660\u0660\u0661",  # Arabic-Indic digits
    ],
)
def test_is_task_id_refuses_everything_else(value: object) -> None:
    from app.domain.tasks import is_task_id

    assert is_task_id(value) is False
