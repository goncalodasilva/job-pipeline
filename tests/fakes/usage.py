"""An in-memory usage meter for executor tests (M2.16; spec 3.7).

Shaped like ``app.modules.ai.UsageMeter``: ``reserve(*, tier, task_type, task_id, kind)``
returns a record id or raises ``BudgetBlocked``; ``complete(record_id, *, success, tokens)``;
``snapshot(snapshot)``. It records every call, the thread it ran on and, in ``events``, the
order of reserves and completions, so a test can line them up with provider calls. No I/O.
"""

import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.domain.errors import BudgetBlocked

RESETS_ON = date(2026, 3, 15)


@dataclass
class MeterRecord:
    id: str
    kind: str
    tier: str
    task_type: str
    task_id: str | None
    completed: bool = False
    success: bool | None = None
    tokens: Any = None
    completions: int = 0


@dataclass
class RecordingMeter:
    """Blocks the request (or search) after ``block_requests_after`` (``block_searches_after``)
    reservations of that kind; ``search_error`` is raised by every search reservation;
    ``snapshot_error`` by every ``snapshot``."""

    block_requests_after: int | None = None
    block_searches_after: int | None = None
    search_error: Exception | None = None
    snapshot_error: Exception | None = None
    events: list[str] = field(default_factory=list)
    records: list[MeterRecord] = field(default_factory=list)
    snapshots: list[Any] = field(default_factory=list)
    threads: list[tuple[str, int]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)  # kinds of reservations that were blocked
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reserve(self, *, tier: str, task_type: str, task_id: str | None, kind: str) -> str:
        with self._lock:
            self.threads.append((f"reserve:{kind}", threading.get_ident()))
            if kind == "search" and self.search_error is not None:
                self.refused.append(kind)
                raise self.search_error
            limit = self.block_searches_after if kind == "search" else self.block_requests_after
            if limit is not None and len(self.of_kind(kind)) >= limit:
                self.refused.append(kind)
                raise BudgetBlocked("Fictional service", RESETS_ON)
            record = MeterRecord(f"record-{len(self.records) + 1}", kind, tier, task_type, task_id)
            self.records.append(record)
            self.events.append(f"reserve:{kind}")
            return record.id

    def complete(self, record_id: str, *, success: bool, tokens: Any) -> None:
        with self._lock:
            self.threads.append(("complete", threading.get_ident()))
            record = next(r for r in self.records if r.id == record_id)
            record.completions += 1
            if not record.completed:  # like the usage service: the first completion wins
                record.completed, record.success, record.tokens = True, success, tokens
            self.events.append(f"complete:{record.kind}")

    def snapshot(self, snapshot: Any) -> None:
        with self._lock:
            self.threads.append(("snapshot", threading.get_ident()))
            if self.snapshot_error is not None:
                raise self.snapshot_error
            self.snapshots.append(snapshot)

    def of_kind(self, kind: str) -> list[MeterRecord]:
        return [record for record in self.records if record.kind == kind]

    @property
    def requests(self) -> list[MeterRecord]:
        return self.of_kind("request")

    @property
    def searches(self) -> list[MeterRecord]:
        return self.of_kind("search")


class CountingUsage:
    """Stands in for the usage service where a test only builds a ``ProviderSetupService``:
    counts the connection test's reserve and complete calls in memory. No I/O."""

    def __init__(self) -> None:
        self.reserved: list[tuple[str, str, bool]] = []  # (service, tier, skip_budget)
        self.completed: list[tuple[str, bool]] = []  # (record id, success)

    def reserve(
        self,
        service: str,
        *,
        tier: str,
        task_id: str | None = None,
        task_type: str | None = None,
        skip_budget: bool = False,
    ) -> str:
        self.reserved.append((service, tier, skip_budget))
        return f"record-{len(self.reserved)}"

    def complete(self, record_id: str, *, success: bool, tokens: Any) -> None:
        self.completed.append((record_id, success))
