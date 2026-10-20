"""What the inbox screen shows, and its versioned shortcut preference (M4.7).

Reads only: a card is marked seen by the triage service when the page posts to it, never here.
"""

from collections.abc import Callable
from datetime import date, tzinfo
from typing import Any
from urllib.parse import urlsplit

from app.domain.clock import Clock
from app.domain.errors import JobLinkInvalid, JobNotFound, format_day, format_short_day
from app.domain.ids import is_safe_id
from app.domain.inbox import InboxFilters
from app.domain.job_intake import checked_link
from app.domain.jobs import Triage, TriageState, effective_state, local_today
from app.domain.settings import SHORTCUTS, ShortcutSettings
from app.modules.storage import Database, inbox_card_repo
from app.services.inbox import PAGE_SIZE, InboxQuery
from app.services.settings import SettingsService, Versioned

_DASH = chr(0x2013)  # an en dash between the ends of a range


class InboxViews:
    def __init__(
        self,
        database: Callable[[], Database | None],
        query: InboxQuery,
        clock: Clock,
        zone: tzinfo,
    ) -> None:
        self._database = database
        self._query = query
        self._clock = clock
        self._zone = zone

    def shortcuts(self) -> Versioned[ShortcutSettings]:
        db = self._database()
        if db is None:
            return Versioned(ShortcutSettings(), 0)
        return SettingsService(db, self._clock).load(SHORTCUTS)

    def save_shortcuts(self, enabled: bool, expected_version: int) -> Versioned[ShortcutSettings]:
        """Save the switch; ``VersionConflict`` when the setting changed since it was shown."""
        db = self._database()
        if db is None:
            raise JobNotFound()
        return SettingsService(db, self._clock).save(
            SHORTCUTS, ShortcutSettings(enabled=enabled), expected_version=expected_version
        )

    def page(self, filters: InboxFilters, *, page: int = 1, selected: str = "") -> dict[str, Any]:
        """One page of the queue and which card is focused. A ``selected`` id that is not on
        the page gives way to the first card."""
        results = self._query.page(filters, page=page)
        if not results.items and page > 1 and results.total > 0:
            # Everything on this page was dealt with: show the last page that has jobs.
            page = (results.total + PAGE_SIZE - 1) // PAGE_SIZE
            results = self._query.page(filters, page=page)
        ids = [item["id"] for item in results.items]
        focused = selected if selected in ids else (ids[0] if ids else "")
        index = ids.index(focused) if focused else 0
        offset = (page - 1) * PAGE_SIZE
        return {
            "results": results,
            "view": filters.view,
            "page": page,
            "selected": focused,
            "position": offset + index + 1,
            "total": results.total,
            "previous_id": ids[index - 1] if index > 0 else "",
            "following_id": ids[index + 1] if index + 1 < len(ids) else "",
            "offset": offset,
            "has_previous_page": page > 1,
            "has_next_page": results.next_card is not None,
            "shortcuts": self.shortcuts(),
        }

    def card(self, job_id: str) -> dict[str, Any]:
        """The values one card shows. Absent values are ``None`` so the template omits them."""
        db = self._database()
        if db is None or not is_safe_id(job_id):
            raise JobNotFound()
        today = local_today(self._clock.now(), self._zone)
        with db.read() as conn:
            found = inbox_card_repo.card(conn, job_id)
        job = found["job"]
        snoozed_until = date.fromisoformat(job["snooze_until"]) if job["snooze_until"] else None
        state = effective_state(
            Triage(TriageState(job["triage_state"]), None, snoozed_until), today
        )
        return {
            **found,
            "state": state.value,
            "work_mode": job["work_mode"].replace("_", " ") if job["work_mode"] else None,
            "salary": _salary(job),
            "experience": _experience(job),
            "posted": _posted(job["posted_date"], today),
            "source": _source(found["sources"]),
            "other_sites": _other_sites(found["sources"]),
            "snoozed_text": format_short_day(snoozed_until) if snoozed_until else None,
        }


def _posted(posted_date: str | None, today: date) -> str | None:
    if not posted_date:
        return None
    posted = date.fromisoformat(posted_date)
    days = (today - posted).days
    if days < 0:
        return f"Posted {format_day(posted)}"
    if days == 0:
        return "Posted today"
    return "Posted yesterday" if days == 1 else f"Posted {days} days ago"


def _links(sources: tuple[tuple[str | None, str], ...]) -> list[str]:
    links = []
    for url, _kind in sources:
        try:
            links.append(checked_link(url) if url else "")
        except JobLinkInvalid:
            continue  # shown as no link, never as a link of unknown shape
    return [link for link in links if link]


def _source(sources: tuple[tuple[str | None, str], ...]) -> dict[str, str] | None:
    """The first source: a checked web link (host shown), else the words "Pasted text"."""
    links = _links(sources)
    if links:
        return {"label": urlsplit(links[0]).hostname or "", "url": links[0]}
    return {"label": "Pasted text", "url": ""} if sources else None


def _other_sites(sources: tuple[tuple[str | None, str], ...]) -> int:
    hosts = {urlsplit(link).hostname for link in _links(sources)}
    return max(0, len(hosts) - 1)


def _amount(value: int | float) -> str:
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"


def _salary(job: dict[str, Any]) -> str | None:
    low, high = job["salary_min"], job["salary_max"]
    if low is None and high is None:
        return None
    if low is not None and high is not None and low != high:
        value = f"{_amount(low)}{_DASH}{_amount(high)}"
    else:
        value = _amount(low if low is not None else high)
    period = f"/ {job['salary_period']}" if job["salary_period"] else None
    return " ".join(part for part in (job["salary_currency"], value, period) if part)


def _experience(job: dict[str, Any]) -> str | None:
    if job["experience_text"]:
        return str(job["experience_text"])
    low, high = job["experience_min"], job["experience_max"]
    if low is None and high is None:
        return None
    if low is not None and high is not None and low != high:
        return f"{low}{_DASH}{high} years"
    return f"{low if low is not None else high} years"
