"""Capture a posting before any AI request; mechanically merge and reversibly split it."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.domain.clock import Clock
from app.domain.errors import FetchRefused, JobLinkInvalid, JobNotFound, JobTextNotFound
from app.domain.ids import IdFactory, is_safe_id
from app.domain.job_intake import DUPLICATE_AT, canonical_url, checked_link, clean_job_text
from app.domain.similarity import words, words_similarity
from app.modules.fetch import FetchLimits, JobPageFetcher, page_text
from app.modules.storage import Database
from app.modules.storage import job_intake_repo as repo
from app.modules.storage.jobs_repo import insert_job

# Bound hostile HTML parsing as well as moving it off the event loop (TD-229).
MAX_PARSE_CHARACTERS: Final = 512 * 1024
MIN_FETCHED_WORDS: Final = 12


@dataclass(frozen=True)
class IntakeResult:
    job_id: str  # the visible card
    source_job_id: str  # the posting retained for a possible split
    merged: bool


class JobIntakeService:
    def __init__(
        self,
        database: Callable[[], Database | None],
        clock: Clock,
        ids: IdFactory,
        fetcher: JobPageFetcher,
    ) -> None:
        self._database = database
        self._clock = clock
        self._ids = ids
        self._fetcher = fetcher

    def _db(self) -> Database:
        db = self._database()
        if db is None:
            raise JobNotFound()
        return db

    async def add_url(self, url: str) -> IntakeResult:
        try:
            checked_link(url)
        except JobLinkInvalid:
            raise FetchRefused() from None
        page = await self._fetcher.fetch(url, FetchLimits())
        if len(page.text) > MAX_PARSE_CHARACTERS:
            raise JobTextNotFound()
        try:
            async with asyncio.timeout(2):
                text = await asyncio.to_thread(page_text, page)
        except TimeoutError:
            raise JobTextNotFound() from None
        text = clean_job_text(text)
        if sum(words(text).values()) < MIN_FETCHED_WORDS:
            raise JobTextNotFound()
        final_url = checked_link(page.final_url)
        return await asyncio.to_thread(self._store, text, final_url, "fetched")

    def add_text(self, text: str, *, link: str | None = None) -> IntakeResult:
        text = clean_job_text(text)
        link = checked_link(link) if link else None
        return self._store(text, link, "pasted")

    def _store(self, text: str, link: str | None, kind: str) -> IntakeResult:
        canonical = canonical_url(link) if link else None
        now = self._clock.now().isoformat()
        with self._db().transaction() as conn:
            jobs = repo.all_jobs(conn)
            by_id = {j.id: j for j in jobs}
            existing = repo.source_job(conn, canonical) if canonical else None
            if existing:
                source_id = existing
                target = by_id[existing].merged_into or existing
            else:
                source_id = self._ids.new()
                target = source_id
                candidates = words(text)
                scores: dict[str, float] = {}
                for job in jobs:
                    root = job.merged_into or job.id
                    score = words_similarity(candidates, words(job.snapshot))
                    if score >= DUPLICATE_AT:
                        scores[root] = max(scores.get(root, 0), score)
                if scores:
                    best = max(scores, key=lambda key: scores[key])
                    # When two groups were explicitly split, only a clear winner may
                    # receive a fresh source. An ambiguous match stays its own card.
                    root_pairs = {
                        tuple(sorted((by_id[a].merged_into or a, by_id[b].merged_into or b)))
                        for a, b in repo.dismissed_pairs(conn)
                    }
                    ambiguous = any(
                        scores[best] - score < 0.05 and tuple(sorted((best, root))) in root_pairs
                        for root, score in scores.items()
                        if root != best
                    )
                    if not ambiguous:
                        target = best
                insert_job(
                    conn,
                    id=source_id,
                    title=text.splitlines()[0][:200],
                    snapshot_text=text,
                    captured_at=now,
                    found_at=now,
                    now=now,
                )
                if target != source_id:
                    repo.set_merge(conn, repo.IntakeJob(source_id, text, None, 1), target, now)
            repo.insert_source(
                conn,
                id=self._ids.new(),
                job_id=source_id,
                url=link,
                canonical=canonical,
                kind=kind,
                now=now,
            )
        return IntakeResult(target, source_id, target != source_id)

    def not_duplicate(self, job_id: str) -> None:
        if not is_safe_id(job_id):
            raise JobNotFound()
        now = self._clock.now().isoformat()
        with self._db().transaction() as conn:
            jobs = {job.id: job for job in repo.all_jobs(conn)}
            job = jobs.get(job_id)
            if job is None:
                raise JobNotFound()
            if job.merged_into is None:
                return
            # Dismiss against the whole former group so a later summary cannot bridge it.
            root = job.merged_into
            for member in jobs.values():
                if member.id != job.id and (member.id == root or member.merged_into == root):
                    repo.dismiss(conn, job.id, member.id, now)
            repo.set_merge(conn, job, None, now)
