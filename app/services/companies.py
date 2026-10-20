"""Companies (M4.10): the sortable list, one company with its jobs, and versioned edits.

Reads change nothing. An unknown or unsafe company id is a ``LookupError``, which the routes
answer with one fixed text that never repeats the id. Company and job text is untrusted data:
nothing here logs it.
"""

from collections.abc import Callable, Mapping
from datetime import date, datetime, tzinfo
from typing import Any

from app.domain.clock import Clock
from app.domain.companies import (
    NAME_TAKEN,
    PAGE_SIZE,
    CompanyEditInvalid,
    checked_edit,
    ordered,
    safe_website,
)
from app.domain.errors import VersionConflict
from app.domain.ids import is_safe_id
from app.domain.job_normalization import company_key
from app.domain.jobs import Triage, TriageState, effective_state, local_today
from app.modules.storage import Database
from app.modules.storage import companies_repo as repo


class CompaniesService:
    def __init__(self, database: Callable[[], Database | None], clock: Clock, zone: tzinfo) -> None:
        self._database, self._clock, self._zone = database, clock, zone

    def _db(self, company_id: str | None = None) -> Database:
        db = self._database()
        if db is None or (company_id is not None and not is_safe_id(company_id)):
            raise LookupError
        return db

    def exists(self, company_id: str) -> bool:
        try:
            db = self._db(company_id)
        except LookupError:
            return False
        with db.read() as conn:
            return repo.get(conn, company_id) is not None

    def listing(self, sort: str, direction: str, page: int) -> dict[str, Any]:
        """One page of companies by ``sort`` and ``direction`` (50 a page; a page past the end
        is the last one)."""
        db = self._database()
        if db is None:
            rows: list[dict[str, Any]] = []
        else:
            with db.read() as conn:
                rows = repo.listing(conn)
        found = ordered(rows, sort, direction)
        pages = max(1, -(-len(found) // PAGE_SIZE))
        page = min(page, pages)
        shown = found[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        return {
            "companies": tuple(
                {**row, "last_day": self._day(row["last_activity"])} for row in shown
            ),
            "total": len(found),
            "page": page,
            "pages": pages,
        }

    def _day(self, stamp: str) -> str:
        try:
            return datetime.fromisoformat(stamp).astimezone(self._zone).date().isoformat()
        except ValueError:
            return stamp[:10]

    def view(self, company_id: str) -> dict[str, Any]:
        db = self._db(company_id)
        today = local_today(self._clock.now(), self._zone)
        with db.read() as conn:
            company = repo.get(conn, company_id)
            if company is None:
                raise LookupError
            jobs = repo.jobs(conn, company_id)
            industries = repo.industries(conn)
        return {
            "company": {**company, "link": safe_website(company["website"])},
            "jobs": tuple({**job, "state": _state(job, today)} for job in jobs),
            "industries": tuple(industries),
        }

    def edit(self, company_id: str, values: Mapping[str, str], *, expected_version: int) -> int:
        """Save the posted fields. ``VersionConflict`` for a stale version, ``CompanyEditInvalid``
        for a field to correct (including a name another company already has by name key);
        nothing is saved in either case. Returns the version now stored."""
        with self._db(company_id).transaction() as conn:
            current = repo.get(conn, company_id)
            if current is None:
                raise LookupError
            if current["version"] != expected_version:
                raise VersionConflict()
            checked = checked_edit(values, current, industries=repo.industries(conn))
            key = current["name_key"]
            if "name" in checked and checked["name"] != current["name"]:
                key = company_key(checked["name"])
                if repo.name_taken(conn, key, company_id):
                    raise CompanyEditInvalid({"name": NAME_TAKEN})
            merged = {**current, **checked}
            if all(merged[field] == current[field] for field in checked):
                return int(current["version"])
            repo.update(
                conn,
                company_id,
                expected_version=expected_version,
                values=merged,
                name_key=key,
                now=self._clock.now().isoformat(),
            )
            return expected_version + 1


def _state(job: Mapping[str, Any], today: date) -> str:
    until = date.fromisoformat(job["snooze_until"]) if job["snooze_until"] else None
    return effective_state(Triage(TriageState(job["triage_state"]), None, until), today).value
