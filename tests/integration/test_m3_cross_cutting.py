"""M3.10 acceptance checks, using only fictional text and temporary workspaces."""

import logging
from pathlib import Path

import pytest

from app.modules.ai.tasks import TASK_SPECS
from tests.asgi_stream import open_stream
from tests.cv_fixtures import fixture_path
from tests.cv_import_harness import importing, said
from tests.leak_scan import RawLogRecords
from tests.refine_harness import refine_path, refining
from tests.tailoring_pages import Tailoring
from tests.waiting import wait_until

pytestmark = pytest.mark.integration
CV = fixture_path("cv.txt").read_text(encoding="utf-8")
HIDDEN = "Confidential fictional Brightwater audit statement."


@pytest.mark.parametrize("task", sorted(TASK_SPECS))
def test_ac_m3_4_every_ai_task_has_a_never_use_request_log_case(tmp_path: Path, task: str) -> None:
    # Explicit cases make a newly registered task fail until its real calling path is covered.
    assert set(TASK_SPECS) == {
        "extract_cv",
        "refine_fact",
        "summarize_job",
        "assess_fit",
        "gap_check",
        "propose_angles",
    }
    with refining(tmp_path) as r:
        hidden = r.typed(HIDDEN, privacy="never_use")
        target = r.target()
        if task == "extract_cv":
            r.run.finished(r.run.paste(CV))
        elif task in {"summarize_job", "assess_fit"}:
            job = r.run.app.state.job_intake.add_text("Fictional gardener role")
            ref = r.run.app.state.job_summary.start(job.source_job_id)
            assert r.run.wait_terminal(ref.id).status.value == "succeeded"
            if task == "assess_fit":
                rows = wait_until(
                    lambda: r.run.rows("SELECT id FROM tasks WHERE type='assess_fit'"),
                    timeout=10,
                    message="fit task after summary",
                )
                assert r.run.wait_terminal(rows[0]["id"]).status.value == "succeeded"
        elif task in {"gap_check", "propose_angles"}:
            # M6.5: the real tailoring service and task over the stored requirements and facts.
            desk = Tailoring(r)
            application = desk.ready("never-use-job")
            if task == "gap_check":
                desk.check(application.id, None)
            else:
                desk.propose(application.id, None)
        else:
            r.started(target.id)
        requests = [request for request in r.run.spy.requests if request.task == task]
        assert requests
        for request in requests:
            assert HIDDEN not in repr(request)
            assert f"F{hidden.number}@" not in repr(request)
        assert r.current(hidden.id).privacy == "never_use"


def test_refine_panel_context_count_uses_the_completed_request(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        target = r.target()
        r.typed("Planned 60 routes a week.")
        r.typed("Handled a fictional confidential audit.", privacy="never_use")
        r.drafts("An unconfirmed draft.")
        r.started(target.id)
        r.typed("This confirmed fact was added after the request.")

        shown = r.client.get(refine_path(target.id), headers=r.run.run.htmx())

        assert "Based on this fact, your answers and 1 other confirmed fact." in said(shown)
        r.answered(target.id, "About 1200 invoices.")
        shown = r.client.get(refine_path(target.id), headers=r.run.run.htmx())
        assert "Based on this fact, your answers and 2 other confirmed facts." in said(shown)


@pytest.mark.parametrize(
    "failure",
    ["wrong_type", "malformed", "oversized", "network", "malformed_ai", "empty_ai", "commit"],
)
def test_ac_m3_7_no_orphan_files_or_rows_after_each_failure_kind(
    tmp_path: Path, failure: str
) -> None:
    with importing(tmp_path) as run:
        if failure in {"wrong_type", "malformed", "oversized"}:
            payload = {
                "wrong_type": b"\x89PNG\r\n\x1a\nfictional image",
                "malformed": b"%PDF-1.7\nfictional broken document",
                "oversized": b"x" * (10 * 1024 * 1024 + 1),
            }[failure]
            response = run.upload("fictional.pdf", payload)
            assert response.status_code in {400, 413}
            run.assert_nothing_stored()
        else:
            if failure == "network":
                run.spy.fail("network")
            elif failure == "malformed_ai":
                run.spy.answer({"invalid": True})
            elif failure == "empty_ai":
                run.spy.answer({"roles": [], "units": []})
            else:
                run.execute(
                    "CREATE TRIGGER fictional_abort BEFORE UPDATE OF status ON tasks "
                    "WHEN NEW.status = 'succeeded' "
                    "BEGIN SELECT RAISE(ABORT, 'fictional failure'); END"
                )
            task = run.finished(run.paste(CV), status="failed")
            assert task.result_ref is None
            run.assert_no_fact_rows()
            stored = run.assert_upload_kept(run.only_import()["id"])
            assert (run.imports_dir / stored["stored_name"]).read_text() == CV
            # The kept file has its row and a visible retry/remove handle; no partial output.
            assert len(run.imports()) == len(run.stored_files()) == 1


def test_cv_and_fact_text_never_in_logs_task_rows_or_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    raw = RawLogRecords()
    raw.install(monkeypatch)
    caplog.set_level(logging.DEBUG)
    cv_marker = "FictionalCvMarkerHazelport"
    fact_marker = "FictionalFactMarkerWillowquay"
    answer_marker = "FictionalAnswerMarkerReedhaven"
    surfaces: list[str] = []
    with refining(tmp_path) as r:
        portal = r.client.portal
        assert portal is not None
        with portal.wrap_async_context_manager(
            open_stream(r.run.app, "/events", port=8000, headers=r.run.run.htmx())
        ) as stream:
            assert stream.status == 200
            frames = [portal.call(stream.next_chunk)]
            r.run.finished(r.run.paste(f"{CV}\n{cv_marker}"))
            target = r.typed(f"Handled invoices. {fact_marker}")
            r.answer_with("questions_only", "ok", "malformed", "malformed")
            r.started(target.id)
            r.answered(target.id, answer_marker)
            # A provider failure carries fact text in its detail and chained exception.
            from app.domain.errors import NetworkUnavailable

            async def fail(request: object, cancel: object) -> object:
                raise NetworkUnavailable(detail=fact_marker) from RuntimeError(answer_marker)

            monkeypatch.setattr(r.run.spy, "run", fail)
            failed = r.typed(f"Another fact. {fact_marker}")
            r.ended_turn(lambda: r.start(failed.id), status="failed")
            # Read the actual authorized stream through the final failure, including headers.
            while not any(b'"status":"failed"' in frame.replace(b" ", b"") for frame in frames):
                frames.append(portal.call(stream.next_chunk))
            surfaces.extend([repr(stream.headers), b"".join(frames).decode()])
        for path in ("/tasks/panel", "/tasks/indicator", "/settings/usage"):
            response = r.client.get(path, headers=r.run.run.htmx())
            assert response.status_code == 200
            surfaces.extend([response.text, repr(response.headers)])
        for table in ("tasks", "usage_records"):
            rows = r.run.rows(f"SELECT * FROM {table}")  # noqa: S608
            assert rows
            surfaces.append(repr(rows))
        assert cv_marker in repr(r.run.rows("SELECT * FROM cv_imports")) or cv_marker in "".join(
            path.read_text() for path in r.run.stored_files()
        )
        assert fact_marker in repr(r.run.rows("SELECT * FROM fact_versions"))
        assert answer_marker in repr(r.run.rows("SELECT * FROM refine_sessions"))
        assert any(
            row["error"] == "NetworkUnavailable" for row in r.run.rows("SELECT * FROM tasks")
        )
    surfaces.extend(text for _where, text in raw.entries)
    for marker in (cv_marker, fact_marker, answer_marker):
        assert all(marker not in surface for surface in surfaces)
