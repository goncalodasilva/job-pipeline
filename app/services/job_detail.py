"""Local job detail reads and edits, with no provider calls or GET writes."""

from collections.abc import Callable, Mapping
from typing import Any

from app.domain.clock import Clock
from app.domain.errors import JobLinkInvalid, JobNotFound, VersionConflict
from app.domain.ids import IdFactory, is_safe_id
from app.domain.job_edit import JobEditInvalid, checked_edits
from app.domain.job_intake import checked_link
from app.domain.job_normalization import company_key
from app.modules.storage import Database, job_detail_repo, job_summary_repo
from app.services.inbox_views import InboxViews


class JobDetailService:
    def __init__(
        self,
        database: Callable[[], Database | None],
        clock: Clock,
        ids: IdFactory,
        views: InboxViews,
    ) -> None:
        self._database, self._clock, self._ids, self._views = database, clock, ids, views

    def _db(self, job_id: str) -> Database:
        db = self._database()
        if not is_safe_id(job_id) or db is None:
            raise JobNotFound()
        return db

    def view(self, job_id: str) -> dict[str, Any]:
        db = self._db(job_id)
        card = self._views.card(job_id)
        with db.read() as conn:
            sources = job_detail_repo.sources(conn, job_id)
            controlled = job_summary_repo.controlled(conn)
        for source in sources:
            try:
                source["url"] = checked_link(source["url"]) if source["url"] else None
            except JobLinkInvalid:
                source["url"] = None
        return {"card": card, "sources": sources, "controlled": controlled}

    def matches(self, job_id: str) -> tuple[dict[str, Any], ...]:
        with self._db(job_id).read() as conn:
            job_summary_repo.job(conn, job_id)
            return job_detail_repo.matches(conn, job_id)

    def edit(self, job_id: str, values: Mapping[str, str], *, expected_version: int) -> int:
        with self._db(job_id).transaction() as conn:
            current = job_summary_repo.job(conn, job_id)
            if current["merged_into_job_id"]:
                raise JobNotFound()
            if current["version"] != expected_version:
                raise VersionConflict()
            checked = checked_edits(values, current)
            if checked.get("company") and not company_key(checked["company"]):
                raise JobEditInvalid({"company": "Enter a company name with letters or numbers."})
            job_detail_repo.update(
                conn,
                job_id,
                expected_version=expected_version,
                values=checked,
                ids=self._ids,
                now=self._clock.now().isoformat(),
            )
            return int(job_summary_repo.job(conn, job_id)["version"])
