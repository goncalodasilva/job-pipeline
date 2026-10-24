"""Queue refreshes remain coherent when a summary merges a selected duplicate."""

import pytest

from app.modules.storage import Database, inbox_repo
from tests.board_pages import by_id
from tests.integration.test_inbox import get, seeded
from tests.refine_harness import refining

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "path,fragment",
    [("/inbox", False), ("/inbox/queue", True), ("/inbox/queue?group_by=company", True)],
    ids=["page", "queue-refresh", "grouped-refresh"],
)
def test_inbox_queue_and_card_share_snapshot_during_duplicate_merge(
    tmp_path, monkeypatch, path, fragment
):
    with refining(tmp_path) as run:
        seeded(run, 2)
        db = run.run.app.state.workspace.db
        original = inbox_repo.page
        merged = False
        # A separate connection can commit under WAL while the queue's read is open.
        # This deterministically reproduces a summary completing between list and card.
        with Database.open(db.path) as writer:

            def merge_after_page(*args, **kwargs):
                nonlocal merged
                result = original(*args, **kwargs)
                if not merged:
                    with writer.transaction() as conn:
                        conn.execute(
                            "UPDATE jobs SET merged_into_job_id='job-0001',version=version+1 "
                            "WHERE id='job-0002'"
                        )
                    merged = True
                return result

            monkeypatch.setattr(inbox_repo, "page", merge_after_page)
            url = path + ("&" if "?" in path else "?") + "selected=job-0002"
            response = get(run, url) if fragment else run.client.get(url)
            assert merged
            assert response.status_code == 200
            assert by_id(response.text, "inbox-card").attrs["data-job-id"] == "job-0002"
            assert 'id="inbox-queue"' in response.text
            # The completed response describes the old snapshot; the next one sees the merge.
            fresh = get(run, url)
            assert fresh.status_code == 200
            assert by_id(fresh.text, "inbox-card").attrs["data-job-id"] == "job-0001"
            assert 'data-job-id="job-0002"' not in fresh.text
            # A direct request for a merged-away card still has its strict fixed 404.
            assert get(run, "/inbox/jobs/job-0002/card").status_code == 404
