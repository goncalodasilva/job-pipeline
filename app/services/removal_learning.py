"""Preview and accept removal rules against a current, transactional inbox snapshot."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, tzinfo

from app.domain.clock import Clock
from app.domain.errors import JobNotFound, VersionConflict
from app.domain.ids import IdFactory, is_safe_id
from app.domain.jobs import local_today
from app.domain.removal_learning import DEFAULT_THRESHOLD, Condition, JobSignals, detect_suggestions
from app.modules.storage import Database, settings_repo
from app.modules.storage import removal_learning_repo as repo

THRESHOLD_KEY = "removal_suggestion_threshold"


@dataclass(frozen=True)
class Preview:
    signature: str
    version: str
    evidence_job_ids: tuple[str, ...]
    explanation: str
    affected: int
    condition: Condition
    origin: str


@dataclass(frozen=True)
class RuleView:
    rule: repo.Rule
    label: str
    evidence: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RulesSettings:
    rules: tuple[RuleView, ...] = ()
    threshold: int = DEFAULT_THRESHOLD
    version: int = 0


def _label(conn: sqlite3.Connection, condition: Condition) -> str:
    company = repo.company_name(conn, condition.value) if condition.field == "company" else None
    return condition.describe(company)


def _threshold(conn: sqlite3.Connection) -> tuple[int, int]:
    setting = settings_repo.get_setting(conn, THRESHOLD_KEY)
    if setting is None:
        return DEFAULT_THRESHOLD, 0
    raw = json.loads(setting.value)
    return (raw if type(raw) is int and 1 <= raw <= 100 else DEFAULT_THRESHOLD), setting.version


def hidden_jobs(jobs: tuple[JobSignals, ...], rules: tuple[repo.Rule, ...]) -> frozenset[str]:
    conditions = {rule.condition for rule in rules if rule.mode == "hide"}
    return frozenset(
        job.id
        for job in jobs
        if job.in_queue and not job.protected and conditions.intersection(job.conditions)
    )


def flag_labels(conn: sqlite3.Connection, job_id: str, today: date) -> tuple[str, ...]:
    rules = tuple(rule for rule in repo.rules(conn, active_only=True) if rule.mode == "flag")
    if not rules:
        return ()
    jobs = repo.signals(conn, today, job_id=job_id)
    return tuple(
        _label(conn, rule.condition)
        for rule in rules
        if jobs and rule.condition in jobs[0].conditions
    )


def _theme_evidence(
    conn: sqlite3.Connection, jobs: tuple[JobSignals, ...]
) -> dict[Condition, tuple[str, ...]]:
    result = repo.latest_theme_run(conn)
    if result is None:
        return {}
    versions = {note.job_id: note.removal_version for note in repo.removal_notes(conn)}
    by_id = {job.id: job for job in jobs}
    contradicted = {condition for job in jobs if job.protected for condition in job.conditions}
    themes: dict[Condition, tuple[str, ...]] = {}
    for theme in result.themes:
        if theme.condition in contradicted:
            continue
        evidence = tuple(
            sorted(
                {
                    id
                    for id, version in theme.evidence
                    if versions.get(id) == version
                    and id in by_id
                    and theme.condition in by_id[id].conditions
                }
            )
        )
        if len(evidence) > len(themes.get(theme.condition, ())):
            themes[theme.condition] = evidence
    return themes


class RemovalLearning:
    def __init__(
        self, database: Callable[[], Database | None], clock: Clock, ids: IdFactory, zone: tzinfo
    ) -> None:
        self._database = database
        self._clock = clock
        self._ids = ids
        self._zone = zone

    def _previews(self, conn: sqlite3.Connection) -> tuple[Preview, ...]:
        jobs = repo.signals(conn, local_today(self._clock.now(), self._zone))
        rules = repo.rules(conn)
        applied = {rule.condition for rule in rules}
        hidden = hidden_jobs(jobs, tuple(rule for rule in rules if rule.active))
        affected: dict[Condition, list[str]] = {}
        for job in jobs:
            if job.in_queue and not job.protected and job.id not in hidden:
                for condition in set(job.conditions):
                    affected.setdefault(condition, []).append(job.id)
        threshold, _ = _threshold(conn)
        dismissals = repo.dismissals(conn)
        sources = {
            item.condition: (
                item.evidence_job_ids,
                item.explanation.removesuffix(item.condition.label + "."),
                "pattern",
            )
            for item in detect_suggestions(jobs, threshold=threshold, dismissals=dismissals)
        }
        for condition, evidence in _theme_evidence(conn, jobs).items():
            minimum = max(threshold, 2 * dismissals.get(condition.signature, 0))
            previous = sources.get(condition)
            if len(evidence) >= minimum and (previous is None or len(evidence) > len(previous[0])):
                sources[condition] = (
                    evidence,
                    f"AI grouped {len(evidence)} removal notes. These jobs match ",
                    "ai",
                )
        versions = {job.id: job.removal_version for job in jobs}
        previews = []
        for condition, (evidence, explanation, origin) in sources.items():
            if condition in applied:
                continue
            matching = sorted(affected.get(condition, ()))
            stamp = [[(id, versions[id]) for id in evidence], matching, explanation, origin]
            version = hashlib.sha256(json.dumps(stamp).encode()).hexdigest()
            previews.append(
                Preview(
                    condition.signature,
                    version,
                    evidence,
                    explanation + _label(conn, condition) + ".",
                    len(matching),
                    condition,
                    origin,
                )
            )
        return tuple(previews)

    def previews(self) -> tuple[Preview, ...]:
        db = self._database()
        if db is None:
            return ()
        with db.read() as conn:
            return self._previews(conn)

    def decide(self, signature: str, version: str, action: str) -> None:
        db = self._database()
        if db is None:
            raise JobNotFound()
        now = self._clock.now().isoformat()
        with db.transaction() as conn:
            preview = next(
                (item for item in self._previews(conn) if item.signature == signature), None
            )
            if preview is None or preview.version != version:
                raise VersionConflict()
            if action == "dismiss":
                repo.dismiss(conn, signature, len(preview.evidence_job_ids), now)
            elif action in ("accept", "hide"):
                repo.insert_rule(
                    conn,
                    self._ids.new(),
                    preview.condition,
                    "hide" if action == "hide" else "flag",
                    preview.evidence_job_ids,
                    now,
                    origin=preview.origin,
                )
            else:
                raise ValueError("Invalid rule action.")

    def settings(self) -> RulesSettings:
        db = self._database()
        if db is None:
            return RulesSettings()
        with db.read() as conn:
            rules = tuple(
                RuleView(
                    rule,
                    _label(conn, rule.condition),
                    repo.evidence_titles(conn, rule.evidence_job_ids),
                )
                for rule in repo.rules(conn)
            )
            threshold, version = _threshold(conn)
            return RulesSettings(rules, threshold, version)

    def change_rule(
        self, id: str, version: int, *, delete: bool, mode: str = "flag", active: bool = False
    ) -> None:
        db = self._database()
        if db is None or not is_safe_id(id):
            raise JobNotFound()
        if mode not in ("flag", "hide"):
            raise ValueError("Invalid rule mode.")
        with db.transaction() as conn:
            if delete:
                existing = next((rule for rule in repo.rules(conn) if rule.id == id), None)
                if existing is None or existing.version != version:
                    raise VersionConflict()
                if existing.evidence_job_ids:
                    repo.dismiss(
                        conn,
                        existing.condition.signature,
                        len(existing.evidence_job_ids),
                        self._clock.now().isoformat(),
                    )
                repo.delete_rule(conn, id, version)
            else:
                repo.update_rule(
                    conn, id, version, mode=mode, active=active, now=self._clock.now().isoformat()
                )

    def change_threshold(self, threshold: int, version: int) -> None:
        db = self._database()
        if db is None:
            raise JobNotFound()
        if not 1 <= threshold <= 100:
            raise ValueError("Choose a threshold from 1 to 100.")
        now = self._clock.now().isoformat()
        with db.transaction() as conn:
            if version == 0:
                saved = settings_repo.insert_setting(
                    conn, THRESHOLD_KEY, json.dumps(threshold), now
                )
            else:
                saved = settings_repo.update_setting(
                    conn, THRESHOLD_KEY, json.dumps(threshold), expected_version=version, now=now
                )
            if not saved:
                raise VersionConflict()
