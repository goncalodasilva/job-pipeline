"""Assemble enrichment demand and focus without writes, tasks or provider calls."""

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, cast

from app.domain.coverage import CoverageData, CoverageEvidence, cluster_requirements, strength
from app.domain.enrichment import BacklogItem, ItemSignals, ItemState, JobDemand, order_backlog
from app.domain.fact_selection import FactRecord, facts_for_ai
from app.domain.quality_hints import quality_hints
from app.domain.skills import normalize_skill
from app.modules.storage import capture_repo, coverage_repo, enrichment_repo, session_sources_repo

FocusKind = Literal["most_useful", "cluster", "job", "role", "skill", "fact", "capture_note"]


@dataclass(frozen=True)
class SessionFocus:
    kind: FocusKind = "most_useful"
    ref: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in (
            "most_useful",
            "cluster",
            "job",
            "role",
            "skill",
            "fact",
            "capture_note",
        ):
            raise ValueError("Choose a session focus.")
        if (self.kind == "most_useful") != (self.ref is None) or (
            self.ref is not None and not self.ref.strip()
        ):
            raise ValueError("Choose a session focus.")


@dataclass(frozen=True)
class SessionItem:
    item: BacklogItem
    signals: ItemSignals
    label: str
    payload: str
    facts: tuple[FactRecord, ...] = ()
    cluster_id: str | None = None
    corpus_stamp: str = ""
    follow_up_of: str | None = None
    personal_answers: tuple[str, ...] = ()

    @property
    def identifier(self) -> str:
        return hashlib.sha256(self.item.key.encode()).hexdigest()

    @property
    def source_stamp(self) -> str:
        """Opaque snapshot id: no wording or source content goes in a task row.

        Include privacy and full current sources, so even ordinary prose cannot be
        replayed after a source is changed or withdrawn. Format changes fail closed.
        """
        snapshot = (
            self.corpus_stamp,
            self.item.kind,
            self.item.target_key,
            self.payload,
            self.facts,
            self.follow_up_of,
            self.personal_answers,
        )
        return hashlib.sha256(repr(snapshot).encode()).hexdigest()


def state_of(row: enrichment_repo.StoredQuestion | None) -> ItemState:
    if row is None:
        return ItemState()
    return ItemState(
        status=cast(Literal["open", "expired"], row.status),
        cooldown_until=datetime.fromisoformat(row.cooldown_until) if row.cooldown_until else None,
        later_until=datetime.fromisoformat(row.later_until) if row.later_until else None,
        later_next_session=row.later_next_session,
        not_sure_count=row.not_sure_count,
        asked_count=row.asked_count,
        last_outcome=cast(
            Literal["answered", "skipped", "not_sure", "later"] | None, row.last_outcome
        ),
        last_asked_at=datetime.fromisoformat(row.last_asked_at) if row.last_asked_at else None,
        earlier_wordings=row.earlier_wordings,
    )


def target_of(row: enrichment_repo.StoredQuestion) -> str:
    return row.cluster_key or row.fact_id or row.capture_note_id or ""


def _live_like_closed_job(
    conn: sqlite3.Connection, coverage: CoverageData, job_id: str
) -> frozenset[str]:
    """Ids of the live requirements that share a cluster with a closed job's requirements.

    A rejected or withdrawn application is out of the coverage clusters (ADR-117 item 4), so a
    session about it asks about what other saved or active jobs still demand of the same things;
    when no other job does, there is nothing to ask. Reads only the closed job's requirements.
    """
    closed = coverage_repo.closed_job_requirements(conn, job_id)
    closed_ids = {r.id for r in closed}
    shared: set[str] = set()
    for cluster in cluster_requirements((*coverage.requirements, *closed), coverage.skills):
        ids = {r.id for r in cluster.requirements}
        if ids & closed_ids:
            shared |= ids - closed_ids
    return frozenset(shared)


def sources(
    conn: sqlite3.Connection,
    focus: SessionFocus,
    now: datetime,
    *,
    resolved_note_draft: str | None = None,
) -> tuple[SessionItem, ...]:
    coverage = coverage_repo.read(conn)
    bank = session_sources_repo.facts(conn)
    notes = capture_repo.list_open(conn)
    # Any historic question may have inherited ordinary prose from another question.
    # A conservative snapshot of the entire available corpus binds those dependencies
    # transitively, across focus changes too. Fact text lives in immutable content
    # versions, so number/version/status/privacy plus current role/skills identify it.
    metadata = tuple(
        (
            f.id,
            f.record.number,
            f.record.version,
            f.record.status,
            f.record.privacy,
            f.record.role,
            f.record.skills,
        )
        for f in bank
    )
    corpus_stamp = hashlib.sha256(repr((coverage, metadata, notes)).encode()).hexdigest()
    if focus.kind == "capture_note" and resolved_note_draft:
        note = capture_repo.get_note(conn, focus.ref or "")
        if note and note.deleted_at is None and note.resolved_fact_id == resolved_note_draft:
            notes = (*notes, note)
    interviews = session_sources_repo.interviews(conn, now.isoformat())
    by_id = {f.id: f for f in bank}
    like_closed: frozenset[str] = frozenset()
    if focus.kind == "job" and not any(r.job_id == focus.ref for r in coverage.requirements):
        like_closed = _live_like_closed_job(conn, coverage, focus.ref or "")
    result = []
    cited: dict[str, list[CoverageEvidence]] = defaultdict(list)
    for link in coverage.evidence:
        cited[link.requirement_id].append(link.fact)
    for cluster in cluster_requirements(coverage.requirements, coverage.skills):
        evidence = tuple(fact for r in cluster.requirements for fact in cited.get(r.id, ()))
        related = tuple(
            by_id[fid].record for fid in sorted({e.id for e in evidence}) if fid in by_id
        )
        if focus.kind == "cluster" and focus.ref not in (cluster.id, cluster.key):
            continue
        if focus.kind == "skill":
            skill = next(
                (
                    s.name
                    for s in coverage.skills
                    if normalize_skill(focus.ref or "")
                    in {normalize_skill(n) for n in (s.name, *s.aliases)}
                ),
                focus.ref or "",
            )
            if cluster.key != "skill:" + normalize_skill(skill):
                continue
        if focus.kind == "job" and not any(
            r.job_id == focus.ref or r.id in like_closed for r in cluster.requirements
        ):
            continue
        if focus.kind in ("fact", "role", "capture_note"):
            continue
        result.append(
            SessionItem(
                BacklogItem(
                    "cluster", cluster.key, strength(evidence), focus.kind != "most_useful"
                ),
                ItemSignals(
                    jobs=tuple(
                        JobDemand(r.job_id, r.job_state, r.kind, interviews.get(r.job_id))
                        for r in cluster.requirements
                    )
                ),
                cluster.label,
                json.dumps(
                    {
                        "label": cluster.label,
                        "requirements": list(dict.fromkeys(r.text for r in cluster.requirements))[
                            :5
                        ],
                    },
                    ensure_ascii=False,
                ),
                related,
                cluster.id,
            )
        )
    for fact in bank:
        record = fact.record
        hints = quality_hints(fact.type, record.content, record.status, record.privacy)
        if (not hints and focus.kind not in ("fact", "role")) or not facts_for_ai((record,)):
            continue
        if focus.kind not in ("most_useful", "fact", "role"):
            continue
        if focus.kind == "fact" and focus.ref != fact.id:
            continue
        if focus.kind == "role" and focus.ref != fact.role_id:
            continue
        result.append(
            SessionItem(
                BacklogItem("quality_hint", fact.id),
                ItemSignals(),
                hints[0] if hints else "Explore this experience?",
                json.dumps({"hints": hints}, ensure_ascii=False),
                (record,),
            )
        )
    if focus.kind in ("most_useful", "capture_note"):
        for note in notes:
            if focus.kind == "capture_note" and note.id != focus.ref:
                continue
            result.append(
                SessionItem(
                    BacklogItem("capture_note", note.id),
                    ItemSignals(),
                    "your note",
                    json.dumps({"note": note.text}, ensure_ascii=False),
                )
            )
    return tuple(replace(entry, corpus_stamp=corpus_stamp) for entry in result)


def backlog(
    conn: sqlite3.Connection,
    focus: SessionFocus,
    now: datetime,
    *,
    exclude: frozenset[str] = frozenset(),
) -> tuple[SessionItem, ...]:
    items = [entry for entry in sources(conn, focus, now) if entry.item.key not in exclude]
    rows = {
        (r.kind + ":" + target_of(r)): state_of(r) for r in enrichment_repo.list_questions(conn)
    }
    states = {key: replace(state, status="open") for key, state in rows.items()}
    ordered = order_backlog(
        (e.item for e in items), {e.item.key: e.signals for e in items}, states, now
    )
    by_key = {e.item.key: e for e in items}
    if focus.kind == "job":
        ordered = tuple(sorted(ordered, key=lambda i: i.strength != "none"))
    return tuple(by_key[i.key] for i in ordered)
