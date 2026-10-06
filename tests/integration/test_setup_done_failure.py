"""Finishing the setup guide when the app can't record it (M1.19 review; AC-M1.7).

When marking the Done step fails (here the database refuses the write), ``POST /setup/done``
answers 422 with the plain "couldn't record" form error, for a plain request and for htmx,
with no ``Location`` or ``HX-Redirect``; the guide stays at Done.

Every folder lives under ``tmp_path``; every value is fictional.
"""

import sqlite3
from pathlib import Path

import pytest

from app.onboarding.progress import SetupProgress
from app.onboarding.steps.done import MSG_SAVE_FAILED
from tests.integration.test_setup_preferences_done import (
    SAVED,
    finish,
    live_statuses,
    required,
    running,
    title_of,
)
from tests.integration.test_setup_redirect import outline

pytestmark = pytest.mark.integration


def test_done_shows_save_failed_error_without_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: SetupProgress, step_id: str) -> None:
        raise sqlite3.OperationalError("attempt to write a readonly database")

    with running(tmp_path, done=("welcome", "provider", "preferences"), preferences=SAVED) as run:
        monkeypatch.setattr(SetupProgress, "mark_done", refuse)
        responses = [finish(run, "add_job"), finish(run, "today", htmx=True)]
        monkeypatch.undo()
        assert "done" not in live_statuses(run)
        assert run.decision.kind == "guide"
        assert run.decision.step == "done"

    for response in responses:
        assert response.status_code == 422, (response.status_code, response.text[:300])
        assert "location" not in response.headers
        assert "hx-redirect" not in response.headers
        error = required(outline(response.text), "form-error")
        assert error.text == MSG_SAVE_FAILED
        assert "readonly" not in response.text
        assert title_of(response).startswith("Error: Done"), title_of(response)
