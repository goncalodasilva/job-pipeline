"""The running app for the Refine with AI tests (M3.8): the CV import harness's app (a finished
setup with the fake provider saved, the real task runner and a spy in front of the provider
wiring builds), plus the refine routes, seeding through the app's own fact service and readers
for the refine sessions.

Assumed routes (launch token and origin; none is a page; a fact id is one path segment of the
safe shape, anything else and an unknown fact answer the fixed 404):

- ``POST /facts/{id}/refine``: start a conversation about the fact, or continue its open one,
  and queue its first turn: the background task ``refine_fact`` with dedupe target
  ``<fact id>``, through the budget gate (the budget dialog's fields and ``tier`` are
  accepted as on the other start routes).
- ``POST /facts/{id}/refine/answer`` (``answer``): add the user's answer and queue the next
  turn; at most three answers per conversation.
- ``POST /facts/{id}/refine/accept`` (``version``: the fact's version as last seen; the wording
  fields of the editor: ``text``, or ``situation``, ``action`` and ``result``; ``acknowledged``
  repeated, once per flagged claim's text the user asserts is true): save the wording as a
  new version of the fact (source ``refine``) and close the conversation.
- ``POST /facts/{id}/refine/discard``: close the conversation; the fact is untouched.

Success answers 200 (htmx, a minimal fragment that M3.9 builds on) or 303 (plain, back to the
fact's editor page). Refusals: 404 unknown fact; 409 a never-use fact, a budget block, a
fourth answer, no open conversation or no suggestion to accept, a stale ``version``; 422 an
accept with a flagged claim neither acknowledged nor edited out, or a blank answer; 403
without the token or origin.

Everything is fictional.
"""

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from httpx import Response

from app.modules.ai import AIRequest
from tests.cv_import_harness import Importing, importing
from tests.fakes.ai import load_ai_fixture

REFINE = "refine_fact"
ACCEPTED = (200, 303)

TARGET = "Cut invoice processing time by 35% across 4 warehouses."
EMPLOYER, TITLE = "Example Widgets Ltd", "Operations Analyst"
CONTEXT = "Planned 60 delivery routes a week with a team of 3."

# Every table a refine must leave as it was, unless the user accepts.
FACT_TABLES = ("roles", "facts", "fact_versions", "fact_events", "skills", "fact_skills")


def refine_path(fact_id: str, action: str = "") -> str:
    return f"/facts/{fact_id}/refine" + (f"/{action}" if action else "")


def claim_texts(raw: str | None) -> list[str]:
    """The texts of a stored claim list (each a string, or an object with ``text``)."""
    items = json.loads(raw or "[]")
    return [item if isinstance(item, str) else str(item["text"]) for item in items]


class Refining:
    def __init__(self, run: Importing) -> None:
        self.run = run

    @property
    def client(self) -> Any:
        return self.run.client

    @property
    def facts(self) -> Any:
        """The open workspace's ``FactService``."""
        service = self.run.app.state.facts
        assert service is not None, "a workspace is open"
        return service

    # --- seeding (through the app's own service) ------------------------------------------------

    def typed(self, text: str, **fields: Any) -> Any:
        from app.services.facts import FactInput

        return self.facts.create(FactInput(text=text, **fields))

    def target(self, **fields: Any) -> Any:
        """The fact most tests refine: confirmed, with its role."""
        from app.services.facts import NewRole

        return self.typed(TARGET, new_role=NewRole(employer=EMPLOYER, title=TITLE), **fields)

    def drafts(self, *texts: str, skills: tuple[str, ...] = ()) -> list[Any]:
        from app.services.facts import DraftInput, DraftOrigin

        origin = DraftOrigin(source="cv_import", provider="fake", model="fake-thorough")
        made = self.facts.create_drafts(
            origin,
            [DraftInput(text=text, skills=skills, original_answer=text) for text in texts],
        )
        return list(made)

    def current(self, fact_id: str) -> Any:
        return self.facts.get(fact_id)

    # --- the provider ---------------------------------------------------------------------------

    def answer_with(self, *fixtures: str) -> None:
        """The refine turns from now on get these fixture answers in turn (the last repeats)."""
        self.run.spy.script(REFINE, *(load_ai_fixture(REFINE, name) for name in fixtures))

    def requests(self) -> list[AIRequest]:
        return [request for request in self.run.spy.requests if request.task == REFINE]

    # --- routes ---------------------------------------------------------------------------------

    def post(self, path: str, data: Any = None, *, htmx: bool = False) -> Response:
        headers = self.run.run.action(**({"HX-Request": "true"} if htmx else {}))
        return self.client.post(path, data=data or {}, headers=headers)

    def start(self, fact_id: str, **values: str) -> Response:
        return self.post(refine_path(fact_id), values)

    def answer(self, fact_id: str, text: str) -> Response:
        return self.post(refine_path(fact_id, "answer"), {"answer": text})

    def accept(
        self,
        fact_id: str,
        *,
        text: str,
        acknowledged: Sequence[str] = (),
        version: int | None = None,
    ) -> Response:
        seen = self.current(fact_id).version if version is None else version
        # A mapping of lists: httpx sends a list of pairs as raw content, not as a form.
        data: dict[str, list[str]] = {"version": [str(seen)], "text": [text]}
        data["acknowledged"] = list(acknowledged)
        return self.post(refine_path(fact_id, "accept"), data)

    def discard(self, fact_id: str) -> Response:
        return self.post(refine_path(fact_id, "discard"))

    # --- turns ----------------------------------------------------------------------------------

    def tasks(self) -> list[Any]:
        rows = self.run.app.state.task_service.recent(50)
        return [row for row in rows if row.type == REFINE]

    def ended_turn(
        self, action: Callable[[], Response], *, status: str = "succeeded"
    ) -> tuple[Response, Any]:
        """Run ``action`` (a start or an answer), which must queue exactly one new turn, and
        wait for that turn to end as ``status``. The clock is frozen, so the new task is the
        one that wasn't there before."""
        before = {row.id for row in self.tasks()}
        response = action()
        assert response.status_code in ACCEPTED, (response.status_code, response.text[:300])
        new = [row for row in self.tasks() if row.id not in before]
        assert len(new) == 1, [(row.id, str(row.status.value)) for row in new]
        row = self.run.wait_terminal(new[0].id)
        assert str(row.status.value) == status, (str(row.status.value), row.error)
        return response, row

    def started(self, fact_id: str, *, status: str = "succeeded") -> Any:
        return self.ended_turn(lambda: self.start(fact_id), status=status)[1]

    def answered(self, fact_id: str, text: str, *, status: str = "succeeded") -> Any:
        return self.ended_turn(lambda: self.answer(fact_id, text), status=status)[1]

    # --- what is stored -------------------------------------------------------------------------

    def sessions(self, fact_id: str) -> list[dict[str, Any]]:
        return self.run.rows(
            "SELECT * FROM refine_sessions WHERE fact_id = ? ORDER BY rowid", (fact_id,)
        )

    def open_session(self, fact_id: str) -> dict[str, Any]:
        found = [row for row in self.sessions(fact_id) if row["state"] == "open"]
        assert len(found) == 1, f"expected one open conversation, found {len(found)}"
        return found[0]

    def versions(self, fact_id: str) -> list[dict[str, Any]]:
        return self.run.rows(
            "SELECT * FROM fact_versions WHERE fact_id = ? ORDER BY version", (fact_id,)
        )

    def fact_tables(self) -> dict[str, list[dict[str, Any]]]:
        """Every row of every fact bank table, to compare before and after."""
        return {
            table: self.run.rows(f"SELECT * FROM {table} ORDER BY rowid")  # noqa: S608
            for table in FACT_TABLES
        }


@contextmanager
def refining(tmp_path: Path, **options: Any) -> Iterator[Refining]:
    with importing(tmp_path, **options) as run:
        yield Refining(run)
