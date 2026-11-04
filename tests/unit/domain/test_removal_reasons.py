"""The fixed list of removal reasons (M9.2 part D; ADR-131 item 6; spec 3.6 row for
``group_removal_reasons``; M9.4 reuses it).

``app.domain.removal_reasons`` is pure Python. ``REMOVAL_REASONS`` is a tuple of frozen
``RemovalReason(key, label, maps_to)`` in the order of the CHECK list on ``jobs.removal_reason``
(migration 0004). ``maps_to`` is a tuple of the job preference fields the reason speaks about,
from ``seniority``, ``location``, ``work_mode``, ``minimum_salary``, ``industry`` and ``company``
(empty when there is none). The keys must equal the CHECK list read from the real schema, so a
reason added to one and not the other fails here. Every value is fictional.
"""

import re
from pathlib import Path
from typing import Any

import pytest

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from tests.fakes.clock import FrozenClock
from tests.integration.test_schema_0004 import START

EXPECTED = {
    "too_junior": ("Too junior", ("seniority",)),
    "too_senior": ("Too senior", ("seniority",)),
    "location": ("Location", ("location", "work_mode")),
    "salary": ("Salary", ("minimum_salary",)),
    "industry": ("Industry", ("industry",)),
    "company": ("Company", ("company",)),
    "not_interested": ("Not interested", ()),
    "other": ("Other", ()),
}
PREFERENCE_FIELDS = {"seniority", "location", "work_mode", "minimum_salary", "industry", "company"}


def reasons() -> Any:
    from app.domain.removal_reasons import REMOVAL_REASONS

    return REMOVAL_REASONS


def check_list_of_the_real_schema(root: Path) -> tuple[str, ...]:
    """The values of ``jobs.removal_reason``'s CHECK, read from a database the real migrations
    made."""
    db = Database.open(root / "app.db")
    try:
        apply_migrations(db, MIGRATIONS_DIR, root / "backups", FrozenClock(START))
        with db.transaction() as conn:
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0]
    finally:
        db.close()
    found = re.search(r"removal_reason\s+TEXT\s+CHECK\s*\(.*?IN\s*\((.*?)\)", sql, re.S)
    assert found is not None, "the jobs table has no removal_reason CHECK list"
    return tuple(re.findall(r"'([a-z_]+)'", found.group(1)))


def test_removal_reasons_are_the_eight_in_the_order_of_the_check_list() -> None:
    assert [r.key for r in reasons()] == list(EXPECTED)


def test_removal_reasons_equal_the_check_list_read_from_the_real_schema(tmp_path: Path) -> None:
    in_schema = check_list_of_the_real_schema(tmp_path)
    assert len(in_schema) == 8
    assert tuple(r.key for r in reasons()) == in_schema


def test_removal_reasons_have_plain_labels_and_the_preference_field_mapping() -> None:
    for reason in reasons():
        label, maps_to = EXPECTED[reason.key]
        assert reason.label == label
        assert tuple(reason.maps_to) == maps_to
        assert set(reason.maps_to) <= PREFERENCE_FIELDS


def test_removal_reasons_are_immutable_and_labels_are_unique_plain_text() -> None:
    items = reasons()
    assert isinstance(items, tuple)
    assert len({r.label for r in items}) == len(items)
    for reason in items:
        assert reason.label == reason.label.strip() and "_" not in reason.label
        with pytest.raises(AttributeError):
            reason.label = "Changed"
        assert isinstance(reason.maps_to, tuple)
