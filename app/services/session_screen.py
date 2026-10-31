"""Read-only session screens and draft actions scoped to an ended session."""

from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from app.domain.clock import Clock
from app.domain.errors import FactNotFound, SessionBusy, SessionNotFound
from app.domain.facts import FactStatus
from app.domain.ids import is_safe_id
from app.domain.settings import AI_PROVIDER, SESSIONS
from app.domain.tasks import ACTIVE, RETRY_FROM
from app.modules.storage import (
    capture_repo,
    coverage_repo,
    enrichment_repo,
    roles_repo,
    session_sources_repo,
    sessions_repo,
    skills_repo,
)
from app.services.fact_views import FactBank, open_bank
from app.services.sessions import SessionService, SessionView, request_notice
from app.services.settings import SettingsService


def phase(view: SessionView) -> str:
    if view.session.state == "ended":
        return "ended"
    if view.task and view.task.status in ACTIVE:
        return "waiting"
    if view.task and view.task.status in RETRY_FROM:
        return "failed"
    return "question" if view.answers and view.answers[-1].outcome == "pending" else "answered"


class SessionScreens:
    def __init__(
        self, sessions: SessionService, bank: Callable[[], FactBank | None], clock: Clock
    ) -> None:
        self.sessions, self._bank, self._clock = sessions, bank, clock

    def new(self, selected: str = "most_useful", length: str = "") -> dict[str, Any]:
        bank = open_bank(self._bank)
        settings = SettingsService(bank.db, self._clock)
        default = settings.load(SESSIONS).value
        choices = [("most_useful", "Most useful")]
        with bank.db.read() as conn:
            for fact in session_sources_repo.facts(conn):
                if (
                    fact.record.status == FactStatus.CONFIRMED
                    and fact.record.privacy != "never_use"
                ):
                    choices.append(("fact:" + fact.id, f"Fact F{fact.record.number}"))
            for role in roles_repo.list_roles(conn):
                choices.append(
                    (
                        "role:" + role.id,
                        "Role: " + " at ".join(p for p in (role.title, role.employer) if p),
                    )
                )
            for skill in skills_repo.list_skills(conn):
                choices.append(("skill:" + skill.name, "Skill: " + skill.name))
            job_ids = sorted({r.job_id for r in coverage_repo.read(conn).requirements})
            job_labels = session_sources_repo.job_labels(conn)
            for jid in job_ids:
                if jid in job_labels:
                    choices.append(("job:" + jid, "Job: " + job_labels[jid]))
            for note in capture_repo.list_open(conn):
                choices.append(("capture_note:" + note.id, "Note: " + note.text[:80]))
            opened = sessions_repo.open_session(conn)
        return {
            "choices": choices,
            "selected": selected if selected in dict(choices) else "most_useful",
            "length": int(length) if length in ("3", "5") else default.length,
            "open_session": opened,
            "configured": settings.load(AI_PROVIDER).value.kind is not None,
            "notice": request_notice(int(length) if length in ("3", "5") else default.length),
        }

    def panel(self, sid: str) -> dict[str, Any]:
        bank = open_bank(self._bank)
        view = self.sessions.view(sid)
        last = view.answers[-1] if view.answers else None
        rows = []
        grouped: dict[str, list[Any]] = {}
        for answer in view.answers:
            if answer.draft_fact_id:
                grouped.setdefault(answer.draft_fact_id, []).append(answer)
        for fid, answers in grouped.items():
            try:
                fact = bank.facts.get(fid)
            except FactNotFound:
                continue
            original = "\n\n".join(a.answer_text for a in answers if a.answer_text is not None)
            merge = None
            with bank.db.read() as conn:
                question = enrichment_repo.get_question(conn, answers[0].question_id)
            if question and question.fact_id and fact.status == FactStatus.DRAFT:
                try:
                    target = bank.facts.get(question.fact_id)
                    if target.status == FactStatus.CONFIRMED and target.privacy != "never_use":
                        merge = {
                            "number": target.number,
                            "url": f"/facts/{fid}/merge?" + urlencode({"target": target.id}),
                        }
                except FactNotFound:
                    pass
            rows.append(
                {
                    "fact": fact,
                    "original": original,
                    "merge": merge,
                    "flags": tuple(
                        {
                            (f["kind"], f["text"]): f for a in answers for f in a.flagged_claims
                        }.values()
                    ),
                    "missing": answers[-1].missing,
                }
            )
        current = next((r for r in rows if last and r["fact"].id == last.draft_fact_id), None)
        state = phase(view)
        signature = ":".join(
            (
                str(view.session.version),
                state,
                view.task.id if view.task else "none",
                str(view.task.attempt) if view.task else "0",
                str(last.version) if last else "0",
                *(f"{r['fact'].id}-{r['fact'].version}" for r in rows),
            )
        )
        return {
            "view": view,
            "last": last,
            "phase": state,
            "signature": signature,
            "drafts": rows,
            "current_draft": current,
            "later_default": SettingsService(bank.db, self._clock)
            .load(SESSIONS)
            .value.ask_later_default,
            "task_key": f"{view.task.id}:{view.task.attempt}" if view.task else None,
            "can_next": state == "answered" and len(view.answers) < view.session.length,
        }

    def draft_action(self, sid: str, fid: str, action: str, version: int) -> None:
        view = self.sessions.view(sid)
        if not is_safe_id(fid) or not any(a.draft_fact_id == fid for a in view.answers):
            raise SessionNotFound()
        if view.session.state != "ended":
            raise SessionBusy()
        facts = open_bank(self._bank).facts
        if action == "confirm":
            facts.confirm(fid, expected_version=version)
        elif action == "discard":
            facts.discard(fid, expected_version=version)
        else:
            raise ValueError("Choose a draft action.")
