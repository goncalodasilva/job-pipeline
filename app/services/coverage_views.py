"""What the Coverage tab shows (M7.7; AC-M7.5): the requirement clusters of the saved and active
jobs, biggest gap first, and the facts behind one Thin cluster. Plain data for the templates.

Everything here reads only (spec 2.9 rule 12): clusters are computed on read from the coverage
sources (ADR-117 item 2, ADR-118) in one read snapshot, and no task, session or request is made.
Requirement wording and fact wording are untrusted text: they are handed on as plain strings
for the templates to escape. Only a cluster id of the exact shape (12 lowercase hex digits)
is ever looked up or put in an address.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urlencode

from app.domain.clock import Clock
from app.domain.coverage import (
    CoverageData,
    CoverageEvidence,
    RequirementCluster,
    Strength,
    cluster_requirements,
    strength,
)
from app.domain.enrichment import ItemSignals, JobDemand, weighted_demand
from app.domain.settings import SESSIONS
from app.modules.storage import coverage_repo, session_sources_repo
from app.services.fact_views import COVERAGE, FactBank, FactItem, fact_item, open_bank, role_labels
from app.services.facts import fact_view
from app.services.settings import SettingsService

PAGE_SIZE: Final = 50
EMPTY_TEXT: Final = "Add a few jobs and their requirements show up here."
NO_RELATED_TEXT: Final = "No confirmed facts are tied to this requirement yet."
NOT_FOUND_TEXT: Final = "This requirement wasn't found. Reload the Coverage tab."
_ID_SHAPE: Final = re.compile(r"[0-9a-f]{12}")
_MAX_PAGE_DIGITS: Final = 6
# Biggest gap first: None, then Thin, then Strong. Symbols tell the strengths apart without
# colour: an empty, a half and a full circle.
_RANK: Final[dict[Strength, int]] = {"none": 0, "thin": 1, "strong": 2}
_WORDS: Final[dict[Strength, str]] = {"none": "None", "thin": "Thin", "strong": "Strong"}
_SYMBOLS: Final[dict[Strength, str]] = {"none": "○", "thin": "◐", "strong": "●"}


@dataclass(frozen=True)
class CoverageRow:
    id: str
    label: str
    strength: Strength
    word: str
    symbol: str
    asked: str
    """"Asked by N saved jobs", with "(M must-have)" when some of those jobs need it."""
    demand: int


@dataclass(frozen=True)
class CoverageView:
    rows: tuple[CoverageRow, ...]
    total: int
    page: int
    pages: int
    length: int
    """The Sessions setting's default length: what Start session posts."""
    count_text: str
    previous_query: str | None
    next_query: str | None

    @property
    def empty(self) -> bool:
        return self.total == 0

    @property
    def empty_text(self) -> str:
        return EMPTY_TEXT


@dataclass(frozen=True)
class CoverageDetail:
    id: str
    label: str
    facts: tuple[FactItem, ...]

    @property
    def no_related_text(self) -> str:
        return NO_RELATED_TEXT


def coverage_page(query: Mapping[str, str]) -> int:
    """The requested page of a request's query; anything that isn't a number is the first."""
    page = query.get("page", "")
    usable = page.isascii() and page.isdigit() and len(page) <= _MAX_PAGE_DIGITS
    return max(1, int(page)) if usable else 1


def _query(page: int) -> str:
    fields = [("tab", COVERAGE)] + ([("page", str(page))] if page > 1 else [])
    return urlencode(fields)


def _jobs(count: int) -> str:
    return "1 saved job" if count == 1 else f"{count} saved jobs"


class CoverageViews:
    """``bank`` answers the open workspace's fact services (None while none is open)."""

    def __init__(self, *, bank: Callable[[], FactBank | None], clock: Clock) -> None:
        self._bank = bank
        self._clock = clock

    def page(self, requested: int = 1) -> CoverageView:
        """One page of the sorted table. A page past the last one is the last."""
        bank = open_bank(self._bank)
        now = self._clock.now()
        with bank.db.read() as conn:
            data = coverage_repo.read(conn)
            interviews = session_sources_repo.interviews(conn, now.isoformat())
        evidence = _evidence_index(data)
        rows = sorted(
            (
                _row(cluster, evidence, interviews, now)
                for cluster in cluster_requirements(data.requirements, data.skills)
            ),
            key=lambda row: (_RANK[row.strength], -row.demand, row.label, row.id),
        )
        pages = max(1, -(-len(rows) // PAGE_SIZE))
        page = min(max(1, requested), pages)
        # Outside the read snapshot above: the setting load only reads, and the length it gives
        # is a default for a form, not a value that has to agree with the rows.
        length = SettingsService(bank.db, self._clock).load(SESSIONS).value.length
        count = "1 requirement." if len(rows) == 1 else f"{len(rows)} requirements."
        return CoverageView(
            rows=tuple(rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]),
            total=len(rows),
            page=page,
            pages=pages,
            length=length,
            count_text=f"{count} Page {page} of {pages}." if pages > 1 else count,
            previous_query=_query(page - 1) if page > 1 else None,
            next_query=_query(page + 1) if page < pages else None,
        )

    def detail(self, cluster_id: str) -> CoverageDetail | None:
        """The confirmed facts that make one cluster's evidence, or None for an id that names
        no cluster (also one of another shape: nothing is looked up for it)."""
        if not _ID_SHAPE.fullmatch(cluster_id):
            return None
        bank = open_bank(self._bank)
        with bank.db.read() as conn:
            data = coverage_repo.read(conn)
            clusters = cluster_requirements(data.requirements, data.skills)
            cluster = next((c for c in clusters if c.id == cluster_id), None)
            if cluster is None:
                return None
            fact_ids = {fact.id for fact in _evidence(cluster, _evidence_index(data))}
            views = sorted(
                (fact_view(conn, fact_id) for fact_id in fact_ids), key=lambda v: v.number
            )
        roles = role_labels(bank)
        return CoverageDetail(cluster.id, cluster.label, tuple(fact_item(v, roles) for v in views))


def _evidence_index(data: CoverageData) -> dict[str, list[CoverageEvidence]]:
    """The evidence links' facts by requirement id, read once for all clusters."""
    index: dict[str, list[CoverageEvidence]] = {}
    for link in data.evidence:
        index.setdefault(link.requirement_id, []).append(link.fact)
    return index


def _evidence(
    cluster: RequirementCluster, index: Mapping[str, list[CoverageEvidence]]
) -> tuple[CoverageEvidence, ...]:
    return tuple(fact for r in cluster.requirements for fact in index.get(r.id, ()))


def _row(
    cluster: RequirementCluster,
    index: Mapping[str, list[CoverageEvidence]],
    interviews: Mapping[str, datetime],
    now: datetime,
) -> CoverageRow:
    found = strength(_evidence(cluster, index))
    signals = ItemSignals(
        jobs=tuple(
            JobDemand(r.job_id, r.job_state, r.kind, interviews.get(r.job_id))
            for r in cluster.requirements
        )
    )
    jobs = {r.job_id for r in cluster.requirements}
    must = {r.job_id for r in cluster.requirements if r.kind == "must_have"}
    asked = f"Asked by {_jobs(len(jobs))}" + (f" ({len(must)} must-have)" if must else "")
    return CoverageRow(
        cluster.id,
        cluster.label,
        found,
        _WORDS[found],
        _SYMBOLS[found],
        asked,
        weighted_demand(signals, now),
    )
