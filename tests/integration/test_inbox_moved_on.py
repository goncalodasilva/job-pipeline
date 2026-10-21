"""M5.4 follow-up: a job counts as To apply only while its application is Preparing.

Once the application moves to any other status the job leaves the To apply view, its count,
the "To apply" state text and the companies' To apply count; it stays out of the New queue and
keeps its triage state, so a refused Undo is still refused. Moving it back to Preparing brings it
back. A To apply job with no application (made before applications existed) still counts.
Everyone and everything here is fictional.
"""

import re

import pytest

from tests.company_pages import table_rows
from tests.integration.test_inbox import get, post, seeded
from tests.refine_harness import refining

pytestmark = pytest.mark.integration
CARD = "/inbox/jobs/{}/triage"
JOB = "job-0001"
COMPANY = "company-0001"


def application_of(r, job_id=JOB):
    [row] = r.run.rows("SELECT id, version FROM applications WHERE job_id=?", (job_id,))
    return row["id"], row["version"]


def move(r, key, job_id=JOB):
    application_id, version = application_of(r, job_id)
    r.run.app.state.applications.move(application_id, key, expected_version=version, source="board")


def apply_for(r, job_id=JOB, version="1"):
    assert post(r, CARD.format(job_id), version=version, action="to_apply").status_code == 200


def heading_count(r, view):
    html = get(r, f"/inbox/queue?view={view}").text
    found = re.search(r'id="inbox-queue-heading">[^<(]*\((\d+)\)', html)
    return int(found.group(1)) if found else 0


def shown_ids(r, view):
    html = get(r, f"/inbox/queue?view={view}").text
    return set(re.findall(r'data-job-id="(job-\d+)"', html))


def triage(r, job_id=JOB):
    [row] = r.run.rows("SELECT triage_state, version FROM jobs WHERE id=?", (job_id,))
    return row["triage_state"], row["version"]


def company_cells(r, company_id=COMPANY):
    for row in table_rows(get(r, "/companies").text)[1:]:
        if f"/companies/{company_id}" in row["hrefs"]:
            return row["cells"]
    raise AssertionError(company_id)


def test_moved_on_job_leaves_to_apply_and_returns_with_preparing(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 3)
        apply_for(r, JOB)
        apply_for(r, "job-0002")
        assert heading_count(r, "to_apply") == 2
        assert shown_ids(r, "to_apply") >= {JOB}

        move(r, "applied")
        assert heading_count(r, "to_apply") == 1
        assert JOB not in get(r, "/inbox/queue?view=to_apply").text
        assert "job-0002" in get(r, "/inbox/queue?view=to_apply").text
        # It does not fall back into the New queue, and its triage state is untouched.
        assert JOB not in get(r, "/inbox/queue?view=queue").text
        assert heading_count(r, "queue") == 1
        assert triage(r) == ("to_apply", 2)

        move(r, "preparing")
        assert heading_count(r, "to_apply") == 2
        assert JOB in get(r, "/inbox/queue?view=to_apply").text
        assert heading_count(r, "queue") == 1


@pytest.mark.parametrize("key", ["applied", "interviewing", "rejected", "withdrawn"])
def test_every_non_preparing_status_leaves_to_apply(tmp_path, key):
    with refining(tmp_path) as r:
        seeded(r, 1)
        apply_for(r)
        move(r, key)
        assert heading_count(r, "to_apply") == 0
        assert heading_count(r, "queue") == 0
        assert "No jobs marked to apply" in get(r, "/inbox/queue?view=to_apply").text


def test_to_apply_job_without_an_application_still_counts(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 2)
        # The legacy shape: marked To apply before applications existed.
        with r.run.app.state.workspace.db.transaction() as conn:
            conn.execute("UPDATE jobs SET triage_state='to_apply' WHERE id=?", (JOB,))
        assert heading_count(r, "to_apply") == 1
        assert JOB in get(r, "/inbox/queue?view=to_apply").text
        assert company_cells(r)[2] == "1"


def test_company_to_apply_count_and_sort_follow_the_preparing_rule(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 3)
        apply_for(r, "job-0002")
        apply_for(r, JOB)
        assert company_cells(r)[2] == "1"
        move(r, "screening")
        cells = company_cells(r)
        assert cells[2] == "0" and cells[1] == "1"  # still an open role at the company
        order = [
            row["hrefs"][0]
            for row in table_rows(get(r, "/companies?sort=to_apply&dir=desc").text)[1:]
        ]
        assert order[0] == "/companies/company-0002"
        move(r, "preparing")
        assert company_cells(r)[2] == "1"


def test_company_page_names_the_status_of_a_moved_on_job(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        apply_for(r)
        assert "To apply" in get(r, f"/companies/{COMPANY}").text
        move(r, "interviewing")
        page = get(r, f"/companies/{COMPANY}").text
        assert "Interviewing" in page and "To apply" not in page.split("role-state")[1]


def test_card_and_drawer_show_the_status_and_no_back_to_queue(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        apply_for(r)
        card = get(r, f"/inbox/jobs/{JOB}/card").text
        assert "To apply" in card and ">Back to queue<" in card

        move(r, "applied")
        for path in (f"/inbox/jobs/{JOB}/card", f"/inbox/jobs/{JOB}/detail"):
            html = get(r, path).text
            assert "Applied" in html, path
            assert ">Back to queue<" not in html, path
            assert 'href="/board"' in html, path
        assert 'class="card-state">To apply' not in get(r, f"/inbox/jobs/{JOB}/card").text
        assert "State: To apply" not in get(r, f"/inbox/jobs/{JOB}/detail").text

        move(r, "preparing")
        # Back in Preparing the job counts as To apply again. Its application now has history,
        # so a Back to queue press is refused by the Undo guard (the refusal prompt covers it);
        # this test does not assert that button either way.
        assert 'class="card-state">To apply' in get(r, f"/inbox/jobs/{JOB}/card").text
        assert "State: To apply" in get(r, f"/inbox/jobs/{JOB}/detail").text


def test_undo_of_a_moved_on_application_is_still_refused(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        apply_for(r)
        move(r, "applied")
        refused = post(r, CARD.format(JOB), version="2", action="undo", view="to_apply")
        assert refused.status_code == 409
        assert triage(r) == ("to_apply", 2)


def test_custom_statuses_follow_their_phase(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 2)
        statuses = r.run.app.state.application_statuses
        researching = statuses.add("Researching", "preparing")
        waiting = statuses.add("Waiting <b>list</b>", "active")
        apply_for(r)
        apply_for(r, "job-0002")
        move(r, researching.key)
        assert heading_count(r, "to_apply") == 2  # a custom Preparing status keeps the job
        assert company_cells(r)[2] == "1"
        move(r, waiting.key)
        assert heading_count(r, "to_apply") == 1  # a custom Active status removes it
        assert JOB not in get(r, "/inbox/queue?view=to_apply").text
        assert company_cells(r)[2] == "0"
        # The custom label is shown on the card and drawer, escaped.
        for path in (f"/inbox/jobs/{JOB}/card", f"/inbox/jobs/{JOB}/detail"):
            html = get(r, path).text
            assert "Waiting &lt;b&gt;list&lt;/b&gt;" in html, path
            assert "<b>list</b>" not in html, path
        assert "Waiting &lt;b&gt;list&lt;/b&gt;" in get(r, f"/companies/{COMPANY}").text


def test_filter_and_grouping_in_to_apply_follow_the_rule(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 22)  # job-0001 and job-0021 are at company-0001
        apply_for(r, JOB)
        apply_for(r, "job-0021")
        apply_for(r, "job-0002")
        path = f"/inbox/queue?view=to_apply&company={COMPANY}&group_by=company"
        before = get(r, path).text
        assert "Fictional Orchard 1 (2)" in before and "To apply (2)" in before
        move(r, "applied")
        after = get(r, path).text
        assert "Fictional Orchard 1 (1)" in after and "To apply (1)" in after
        assert JOB not in after and "job-0021" in after
        grouped = get(r, "/inbox/queue?view=to_apply&group_by=company").text
        assert "Fictional Orchard 1 (1)" in grouped and "Fictional Orchard 2 (1)" in grouped
        assert "To apply (2)" in grouped


def test_drawer_undo_after_the_application_moved_on_offers_the_board(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        apply_for(r)
        move(r, "applied")
        refused = post(r, f"/inbox/jobs/{JOB}/detail-triage", version="2", action="undo")
        assert refused.status_code == 409
        assert "This application already has changes" in refused.text
        assert 'href="/board"' in refused.text and "Open the Board" in refused.text
        assert triage(r) == ("to_apply", 2)
