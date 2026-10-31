"""A session start re-checks its first backlog read only when something was written since.

The check inside the enqueue transaction (ADR-121) must still catch a change made between the
first read and the enqueue (here, during the budget gate), and must not refuse a start because
of a write that leaves the first item as it was.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.domain.errors import VersionConflict
from app.services.sessions import SessionFocus
from tests.integration.test_sessions import seed_note, service
from tests.refine_harness import Refining, refining

pytestmark = pytest.mark.integration


@pytest.fixture
def desk(tmp_path: Path) -> Iterator[Refining]:
    with refining(tmp_path) as r:
        yield r


def write_during_gate(desk: Refining, sql: str, params: tuple[object, ...] = ()) -> None:
    """Run ``sql`` once, right after the first backlog read, from inside the budget gate."""
    gate = desk.run.app.state.budget_gate
    original = gate.check

    def check(*args: object, **kwargs: object) -> object:
        desk.run.execute(sql, params)
        return original(*args, **kwargs)

    gate.check = check  # type: ignore[method-assign]


def sessions(desk: Refining) -> list[dict[str, object]]:
    return desk.run.rows("SELECT id FROM sessions")


def test_start_refuses_when_the_item_changed_after_the_first_read(desk: Refining) -> None:
    focus = seed_note(desk)
    write_during_gate(
        desk, "UPDATE capture_notes SET text='Changed meanwhile.' WHERE id=?", ("session-note",)
    )
    with pytest.raises(VersionConflict):
        service(desk).start(focus, 3)
    assert sessions(desk) == []
    assert desk.run.rows("SELECT id FROM tasks WHERE type='enrichment_question'") == []


def test_start_refuses_when_the_item_was_removed_after_the_first_read(desk: Refining) -> None:
    focus = seed_note(desk)
    write_during_gate(desk, "DELETE FROM capture_notes WHERE id=?", ("session-note",))
    with pytest.raises(VersionConflict):
        service(desk).start(focus, 3)
    assert sessions(desk) == []


def test_start_goes_ahead_after_a_write_that_leaves_the_item_unchanged(desk: Refining) -> None:
    focus = seed_note(desk)
    write_during_gate(
        desk,
        "INSERT INTO settings(key,value,version,created_at,updated_at) "
        "VALUES ('unrelated','{}',1,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
    )
    started = service(desk).start(focus, 3)
    assert started.session_id is not None and started.task is not None
    assert len(sessions(desk)) == 1


def test_start_with_the_default_focus_is_unchanged_without_writes(desk: Refining) -> None:
    seed_note(desk)
    started = service(desk).start(SessionFocus(), 3)
    assert started.session_id is not None


def count_backlog_reads(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace the backlog read ``SessionService`` uses with one that counts its calls made on
    this thread (``start`` runs on the caller's; the question task reads on a worker's)."""
    import threading

    import app.services.sessions as module

    caller = threading.get_ident()

    calls: list[object] = []
    real = module.backlog

    def counting(*args: object, **kwargs: object) -> object:
        if threading.get_ident() == caller:
            calls.append(args[1] if len(args) > 1 else kwargs.get("focus"))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "backlog", counting)
    return calls


def test_start_reads_the_backlog_once_when_nothing_was_written(
    desk: Refining, monkeypatch: pytest.MonkeyPatch
) -> None:
    focus = seed_note(desk)
    calls = count_backlog_reads(monkeypatch)
    started = service(desk).start(focus, 3)
    assert started.session_id is not None
    assert len(calls) == 1, "no write between the read and the enqueue: no second read"


def test_start_reads_the_backlog_again_after_a_write_during_the_gate(
    desk: Refining, monkeypatch: pytest.MonkeyPatch
) -> None:
    focus = seed_note(desk)
    write_during_gate(
        desk,
        "INSERT INTO settings(key,value,version,created_at,updated_at) "
        "VALUES ('unrelated','{}',1,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
    )
    calls = count_backlog_reads(monkeypatch)
    started = service(desk).start(focus, 3)
    assert started.session_id is not None
    assert len(calls) == 2, "a write since the first read: the enqueue looks again"
