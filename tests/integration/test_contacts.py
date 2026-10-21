"""M5.6: contacts, interactions and company history.

The built interface (its markup is described in ``tests/contact_pages.py``). Every route below
needs the launch token, and each POST also needs the origin; an unknown field is 400. Contact
text is untrusted data and is escaped everywhere.

- ``POST /companies/{company id}/contacts`` adds a contact (``name`` required; ``role``,
  ``email``, ``phone``, ``relationship``, ``notes`` optional). 200 answers the
  ``#company-contacts`` block, 422 the form with each error tied to its field, and an unknown
  company is the fixed 404 "This company wasn't found.".
- ``POST /companies/{company id}/contacts/{contact id}`` edits one (the same fields and
  ``version``): 200, 422, or 409 with the reload prompt. A contact of another company, an unknown
  contact and an unsafe id are 404; a missing or non-numeric ``version`` is 400.
- ``POST /applications/{id}/contacts`` (``contact_id``) links a contact of the application's
  company; anything else (another company's contact, an unknown or unsafe id) is 422 tied to
  ``contact_id`` and changes nothing. Linking twice is harmless.
  ``POST /applications/{id}/contacts/new`` adds a contact to the application's company and links
  it in one step (the contact fields). ``POST /applications/{id}/contacts/{contact id}/unlink``
  (no fields) removes the link only; contacts are never deleted in M5.
- ``POST /applications/{id}/interactions`` logs an interaction: ``date`` (``YYYY-MM-DD``),
  ``channel`` (``email``, ``call``, ``video``, ``in_person``, ``message`` or ``other``), ``note``
  and an optional ``contact_id`` (a contact of the application's company; otherwise 422).
  All of these answer the ``#application-detail`` block (200) like the notes route; a refusal
  is 422 and changes nothing.
"""

from datetime import timedelta

import pytest

from app.domain.errors import ApplicationNotFound
from tests.application_desk import UNDO_REFUSED
from tests.board_pages import by_id, elements
from tests.contact_pages import (
    CHANNELS,
    at_clock,
    company_id_of,
    contact_items,
    contact_values,
    form_actions,
    history_items,
    hrefs_in,
    insert_contact,
    insert_interaction,
    interaction_values,
    link_contact,
    mailto_hrefs,
    option_values,
    parsed,
    tied_error,
)
from tests.cv_import_harness import importing
from tests.integration.test_board import seeder as new_seeder
from tests.integration.test_board import snapshot, version_of
from tests.integration.test_inbox import get, post
from tests.integration.test_job_summary import OUTPUT as SUMMARY_OUTPUT
from tests.integration.test_job_summary import answer as summary_answer
from tests.integration.test_job_summary import seed as summary_seed
from tests.integration.test_job_summary import start as summary_start
from tests.refine_harness import refining

pytestmark = pytest.mark.integration
COMPANY_NOT_FOUND = "This company wasn't found."
CONTACT_NOT_FOUND = "This contact wasn't found."
LARKSPUR, ELM = "Larkspur Orchards", "Elm Fictional Yard"


def seeder(r):
    """One seeder per app, so a company named twice is one company."""
    if not hasattr(r, "desk"):
        r.desk = new_seeder(r)
    return r.desk


def db(r):
    return r.run.app.state.workspace.db


def application(r, job="contact-job", title="Seed Buyer", company=LARKSPUR, status="applied"):
    return seeder(r).application(job, title, company, status)


def company_of(r, name=LARKSPUR):
    return company_id_of(db(r), name)


def contacts(r):
    return r.run.rows("SELECT * FROM contacts ORDER BY name, id")


def links(r):
    return [
        (row["application_id"], row["contact_id"])
        for row in r.run.rows(
            "SELECT application_id, contact_id FROM application_contacts ORDER BY 1, 2"
        )
    ]


def interactions(r):
    return r.run.rows("SELECT * FROM interactions ORDER BY happened_at, id")


def detail(r, aid):
    return get(r, f"/applications/{aid}/detail").text


def company_page(r, cid):
    return r.client.get(f"/companies/{cid}").text


# --- add, edit, versions and field-tied 422s ------------------------------------------------------


def test_contact_add_edit_versioned_and_422_tied_to_fields(tmp_path):
    with refining(tmp_path) as r:
        seeder(r).job("contact-job", "Seed Buyer", LARKSPUR)
        cid = company_of(r)
        add_path = f"/companies/{cid}/contacts"

        # Add: trimmed text is stored on the company with version 1 and the frozen clock.
        added = post(r, add_path, **contact_values(name="  Sam Fictional  "))
        assert added.status_code == 200 and 'id="company-contacts"' in added.text
        [row] = contacts(r)
        assert row["company_id"] == cid and row["name"] == "Sam Fictional" and row["version"] == 1
        assert (row["role"], row["email"], row["phone"]) == (
            "Hiring manager",
            "sam@example.test",
            "+00 555 0100",
        )
        assert row["relationship"] == "Met at a fictional job fair"
        assert row["notes"] == "Prefers a short email."
        assert parsed(row["created_at"]) == r.run.clock.now()
        assert row["id"] in contact_items(added.text)
        assert "Contact saved." in added.text

        # Edit with the current version: saved, version 2, updated_at moves.
        edit_path = f"{add_path}/{row['id']}"
        r.run.clock.advance(timedelta(minutes=5))
        edited = post(r, edit_path, version="1", **contact_values(role="Head of hiring"))
        assert edited.status_code == 200 and "Head of hiring" in edited.text
        [row] = contacts(r)
        assert (row["role"], row["version"]) == ("Head of hiring", 2)
        assert parsed(row["updated_at"]) == r.run.clock.now() > parsed(row["created_at"])

        # A stale version is 409 with the reload prompt and changes nothing.
        before = snapshot(r)
        stale = post(r, edit_path, version="1", **contact_values(role="Lost change"))
        assert stale.status_code == 409 and "Reload" in stale.text
        assert snapshot(r) == before

        # A missing or non-numeric version is a malformed request.
        assert post(r, edit_path, **contact_values()).status_code == 400
        assert post(r, edit_path, version="abc", **contact_values()).status_code == 400
        assert snapshot(r) == before

        # An email needs only a valid shape (no delivery check); an empty email is fine.
        for email in ("sam.o+tag@mail.example.test", "someone@nonexistent.invalid", ""):
            version = str(contacts(r)[0]["version"])
            saved = post(r, edit_path, version=version, **contact_values(email=email))
            assert saved.status_code == 200, email
            assert contacts(r)[0]["email"] == email

        # 422s are tied to their fields on add and on edit; the typed values stay and nothing is
        # saved.
        refused = [
            ({"name": "   "}, ["name"]),
            ({"email": "not-an-email"}, ["email"]),
            ({"email": "two@@example.test"}, ["email"]),
            ({"email": "spaces in@example.test"}, ["email"]),
            ({"email": "nobody@"}, ["email"]),
            ({"name": "", "email": "bad-email"}, ["name", "email"]),
        ]
        for overrides, fields in refused:
            for path, extra in ((add_path, {}), (edit_path, {"version": str(version_now(r))})):
                before = snapshot(r)
                answer = post(r, path, **extra, **contact_values(**overrides))
                assert answer.status_code == 422, (overrides, path)
                for field in fields:
                    assert tied_error(answer.text, field), (overrides, field)
                for field in {"name", "email"} - set(fields):
                    assert not tied_error(answer.text, field), (overrides, field)
                for typed in overrides.values():
                    assert typed.strip() == "" or typed in answer.text
                assert snapshot(r) == before

        # Unknown fields, unknown or unsafe ids and another company's contact.
        assert post(r, add_path, **contact_values(), extra="bad").status_code == 400
        missing = post(r, "/companies/missing/contacts", **contact_values())
        assert missing.status_code == 404 and missing.text == COMPANY_NOT_FOUND
        seeder(r).job("elm-job", "Pallet Driver", ELM)
        other = company_of(r, ELM)
        before = snapshot(r)
        for path in (
            f"/companies/{other}/contacts/{row['id']}",
            f"{add_path}/missing",
            f"{add_path}/bad.id",
        ):
            answer = post(r, path, version="3", **contact_values())
            assert answer.status_code == 404, path
            assert row["id"] not in answer.text and "bad.id" not in answer.text
        assert snapshot(r) == before


def version_now(r):
    return contacts(r)[0]["version"]


# --- linking and unlinking in the drawer ----------------------------------------------------------


def test_link_contact_only_from_the_application_company(tmp_path):
    with refining(tmp_path) as r:
        a = application(r)
        seeder(r).job("elm-job", "Pallet Driver", ELM)
        mine = insert_contact(db(r), "contact-mine", company_of(r), "Sam Fictional")
        theirs = insert_contact(db(r), "contact-theirs", company_of(r, ELM), "Elm Fictional")
        path = f"/applications/{a.id}/contacts"

        # The drawer offers only this company's contacts that are not linked yet.
        offered = option_values(detail(r, a.id), "contact_id", within="application-contacts")
        assert mine in offered and theirs not in offered
        assert f"/applications/{a.id}/contacts" in form_actions(detail(r, a.id))

        linked = post(r, path, contact_id=mine)
        assert linked.status_code == 200 and 'id="application-detail"' in linked.text
        assert links(r) == [(a.id, mine)]
        shown = contact_items(detail(r, a.id), "application-contacts")
        assert list(shown) == [mine] and "Sam Fictional" in shown[mine].text
        assert f"/applications/{a.id}/contacts/{mine}/unlink" in form_actions(detail(r, a.id))
        assert mine not in option_values(
            detail(r, a.id), "contact_id", within="application-contacts"
        )

        # Another company's contact, an unknown id and an unsafe id are 422 tied to the field.
        before = snapshot(r)
        for bad in (theirs, "missing", "<script>", ""):
            answer = post(r, path, contact_id=bad)
            assert answer.status_code == 422, bad
            assert tied_error(answer.text, "contact_id"), bad
            assert "<script>" not in answer.text
        assert snapshot(r) == before

        # Linking twice never fails the server and never doubles the link.
        assert post(r, path, contact_id=mine).status_code == 200
        assert links(r) == [(a.id, mine)]

        # Unlinking removes only the link; the contact stays and can be linked again.
        unlink = post(r, f"{path}/{mine}/unlink")
        assert unlink.status_code == 200 and links(r) == []
        assert {c["id"] for c in contacts(r)} == {mine, theirs}
        assert mine in option_values(detail(r, a.id), "contact_id", within="application-contacts")
        assert post(r, f"{path}/{mine}/unlink").status_code == 200  # nothing left to remove
        assert links(r) == []
        assert post(r, path, contact_id=mine).status_code == 200 and links(r) == [(a.id, mine)]

        # Unlinking a contact that is not linked (another company's too) changes no link.
        assert post(r, f"{path}/{theirs}/unlink").status_code == 200
        assert links(r) == [(a.id, mine)]
        assert {c["id"] for c in contacts(r)} == {mine, theirs}

        # An id that can't name a contact is a fixed 404 that never repeats it.
        for unsafe in ("bad.id", "%3Cscript%3E", "x" * 65):
            answer = post(r, f"{path}/{unsafe}/unlink")
            assert answer.status_code == 404 and answer.text == CONTACT_NOT_FOUND, unsafe
            assert unsafe not in answer.text
        assert links(r) == [(a.id, mine)]


def test_inline_contact_in_the_drawer_is_added_to_the_company_and_linked(tmp_path):
    with refining(tmp_path) as r:
        a = application(r)
        path = f"/applications/{a.id}/contacts/new"
        hint = "Add a contact above to pick one"
        assert hint in by_id(detail(r, a.id), "interaction-form").text
        added = post(r, path, **contact_values(name="Pat Fictional"))
        assert hint not in by_id(detail(r, a.id), "interaction-form").text
        assert added.status_code == 200 and 'id="application-detail"' in added.text
        [row] = contacts(r)
        assert row["company_id"] == company_of(r) and row["name"] == "Pat Fictional"
        assert links(r) == [(a.id, row["id"])]
        before = snapshot(r)
        refused = post(r, path, **contact_values(name=" ", email="bad-email"))
        assert refused.status_code == 422
        assert tied_error(refused.text, "name") and tied_error(refused.text, "email")
        assert snapshot(r) == before
        assert post(r, "/applications/missing/contacts/new", **contact_values()).status_code == 404


# --- interactions ---------------------------------------------------------------------------------


def test_interaction_logged_on_timeline_and_sets_last_contact(tmp_path):
    with refining(tmp_path) as r:
        a = application(r)
        cid = company_of(r)
        sam = insert_contact(db(r), "contact-sam", cid, "Sam Fictional")
        pat = insert_contact(db(r), "contact-pat", cid, "Pat Fictional")
        link_contact(db(r), a.id, sam)
        path = f"/applications/{a.id}/interactions"
        assert path in form_actions(detail(r, a.id))
        assert set(option_values(detail(r, a.id), "contact_id", within="interaction-form")) >= {
            "",
            sam,
            pat,
        }
        channels = option_values(detail(r, a.id), "channel", within="interaction-form")
        assert set(channels) >= set(CHANNELS)

        # Nobody has been contacted yet.
        assert "Last contact: 2" not in contact_items(company_page(r, cid))[sam].text

        r.run.clock.advance(timedelta(minutes=5))
        logged = post(r, path, **interaction_values(contact_id=sam))
        assert logged.status_code == 200 and 'id="application-detail"' in logged.text
        [row] = interactions(r)
        assert (row["contact_id"], row["application_id"], row["channel"]) == (sam, a.id, "call")
        assert row["note"] == "Talked about the fictional orchard team."
        assert row["version"] == 1 and parsed(row["created_at"]) == r.run.clock.now()

        # The timeline shows it, with its date, note and contact.
        timeline = [
            e
            for e in elements(detail(r, a.id))
            if e.tag == "li" and e.attrs.get("data-timeline-kind") == "interaction"
        ]
        assert len(timeline) == 1
        assert "2026-10-18" in timeline[0].text and "orchard team" in timeline[0].text
        assert "Sam Fictional" in timeline[0].text
        assert "Interaction logged." in logged.text

        # The company page shows the latest interaction as the contact's last contact.
        assert "Last contact: 2026-10-18" in contact_items(company_page(r, cid))[sam].text
        earlier = post(
            r, path, **interaction_values(date="2026-10-10", channel="email", contact_id=sam)
        )
        assert earlier.status_code == 200
        assert "Last contact: 2026-10-18" in contact_items(company_page(r, cid))[sam].text
        later = post(
            r, path, **interaction_values(date="2026-10-25", channel="video", contact_id=sam)
        )
        assert later.status_code == 200
        items = contact_items(company_page(r, cid))
        assert "Last contact: 2026-10-25" in items[sam].text
        assert "Last contact: 2" not in items[pat].text

        # An interaction without a contact is on the timeline and changes nobody's last contact.
        before_pat = items[pat].text
        alone = post(r, path, **interaction_values(date="2026-10-30", note="Left a voicemail."))
        assert alone.status_code == 200 and "Left a voicemail." in detail(r, a.id)
        assert contact_items(company_page(r, cid))[pat].text == before_pat
        assert len(interactions(r)) == 4

        # Every channel is accepted.
        for channel in CHANNELS:
            assert post(r, path, **interaction_values(channel=channel)).status_code == 200


def test_interaction_refusals_are_tied_to_fields_and_change_nothing(tmp_path):
    with refining(tmp_path) as r:
        a = application(r)
        seeder(r).job("elm-job", "Pallet Driver", ELM)
        theirs = insert_contact(db(r), "contact-theirs", company_of(r, ELM), "Elm Fictional")
        path = f"/applications/{a.id}/interactions"
        before = snapshot(r)
        cases = [
            ({"date": "not a date"}, "date"),
            ({"date": "2026-02-30"}, "date"),
            ({"date": ""}, "date"),
            ({"channel": "carrier-pigeon"}, "channel"),
            ({"channel": ""}, "channel"),
            ({"contact_id": theirs}, "contact_id"),
            ({"contact_id": "missing"}, "contact_id"),
        ]
        for overrides, field in cases:
            answer = post(r, path, **interaction_values(**overrides))
            assert answer.status_code == 422, overrides
            assert tied_error(answer.text, field), overrides
            assert snapshot(r) == before
        assert post(r, path, **interaction_values(), extra="bad").status_code == 400
        missing = post(r, "/applications/missing/interactions", **interaction_values())
        assert missing.status_code == 404 and missing.text == ApplicationNotFound.message
        assert snapshot(r) == before


# --- company history ------------------------------------------------------------------------------


def seed_history(r):
    set_clock = at_clock(r)
    s = seeder(r)
    set_clock("2026-09-01T12:00:00+00:00")
    first = s.application("history-1", "Seed Buyer", LARKSPUR, "applied")
    set_clock("2026-09-10T12:00:00+00:00")
    r.run.app.state.applications.move(
        first.id, "rejected", expected_version=version_of(r, first.id), source="board"
    )
    set_clock("2026-10-05T12:00:00+00:00")
    second = s.application("history-2", "Irrigation Planner", LARKSPUR, "interviewing")
    set_clock("2026-10-06T12:00:00+00:00")
    third = s.application("history-3", "Frost Watcher", LARKSPUR, "preparing")
    set_clock("2026-10-07T12:00:00+00:00")
    elsewhere = s.application("history-4", "Pallet Driver", ELM, "applied")
    return first, second, third, elsewhere


def test_company_history_lists_applications_with_dates_and_outcomes(tmp_path):
    with refining(tmp_path) as r:
        first, second, third, elsewhere = seed_history(r)
        at_clock(r)("2026-10-21T09:00:00+00:00")
        page = company_page(r, company_of(r))
        items = history_items(page)
        assert set(items) == {first.id, second.id, third.id}  # not another company's
        assert elsewhere.id not in page

        assert "Closed: rejected" in items[first.id].text
        assert (
            "Applied: 2026-09-01" in items[first.id].text and "Seed Buyer" in items[first.id].text
        )
        assert "Interviewing" in items[second.id].text
        assert "Applied: 2026-10-05" in items[second.id].text
        assert "Irrigation Planner" in items[second.id].text
        assert "Preparing" in items[third.id].text and "Applied: Not yet" in items[third.id].text
        for one in (first, second, third):
            assert f"/applications/{one.id}" in hrefs_in(page, items[one.id])

        # Newest applied first.
        order = list(items)
        assert order.index(second.id) < order.index(first.id)

        # A status change shows in words on the next read; reading changes nothing.
        r.run.app.state.applications.move(
            second.id, "rejected", expected_version=version_of(r, second.id), source="board"
        )
        before = snapshot(r)
        again = history_items(company_page(r, company_of(r)))
        assert "Closed: rejected" in again[second.id].text
        assert "Applied: 2026-10-05" in again[second.id].text
        assert snapshot(r) == before

        # The other company has its own history.
        other = history_items(company_page(r, company_of(r, ELM)))
        assert set(other) == {elsewhere.id}


def test_reapply_note_counts_last_30_days_and_never_blocks(tmp_path):
    with refining(tmp_path) as r:
        set_clock = at_clock(r)
        s = seeder(r)
        set_clock("2026-09-26T12:00:00+00:00")  # 45 days before the reads below
        s.application("old-1", "Seed Buyer", LARKSPUR, "applied")
        set_clock("2026-10-12T12:00:00+00:00")  # 29 days before
        s.application("recent-1", "Irrigation Planner", LARKSPUR, "applied")
        set_clock("2026-11-09T12:00:00+00:00")
        s.application("elm-1", "Pallet Driver", ELM, "applied")
        set_clock("2026-11-10T12:00:00+00:00")
        cid = company_of(r)

        # One application in the window (plus an old one): no note.
        page = company_page(r, cid)
        assert by_id(page, "company-reapply-note") is None
        assert "in the last 30 days" not in page

        # A second one inside the window: a gentle note with the count. Nothing stops anyone from
        # applying: the Apply route and the status move both answer 200.
        s.job("recent-2", "Frost Watcher", LARKSPUR)
        version = r.run.rows("SELECT version FROM jobs WHERE id='recent-2'")[0]["version"]
        apply = post(r, "/inbox/jobs/recent-2/triage", version=str(version), action="to_apply")
        assert apply.status_code == 200
        [created] = r.run.rows("SELECT id FROM applications WHERE job_id='recent-2'")
        moved = post(
            r,
            f"/applications/{created['id']}/status",
            version=str(version_of(r, created["id"])),
            status="applied",
            source="menu",
        )
        assert moved.status_code == 200
        page = company_page(r, cid)
        note = by_id(page, "company-reapply-note")
        assert note is not None and "2 applications here in the last 30 days" in note.text

        # A third makes it three; the window is a fixed 30 days (the 45-day-old one never counts).
        s.job("recent-3", "Pruning Lead", LARKSPUR)
        version = r.run.rows("SELECT version FROM jobs WHERE id='recent-3'")[0]["version"]
        assert (
            post(
                r, "/inbox/jobs/recent-3/triage", version=str(version), action="to_apply"
            ).status_code
            == 200
        )
        [third] = r.run.rows("SELECT id FROM applications WHERE job_id='recent-3'")
        assert (
            post(
                r,
                f"/applications/{third['id']}/status",
                version=str(version_of(r, third["id"])),
                status="applied",
                source="menu",
            ).status_code
            == 200
        )
        note = by_id(company_page(r, cid), "company-reapply-note")
        assert note is not None and "3 applications here in the last 30 days" in note.text

        # The other company has one recent application: no note there. Time moving on ends it.
        assert by_id(company_page(r, company_of(r, ELM)), "company-reapply-note") is None
        set_clock("2027-01-15T12:00:00+00:00")
        assert by_id(company_page(r, cid), "company-reapply-note") is None


# --- tokens, origin and escaping ------------------------------------------------------------------


def test_contact_routes_need_token_posts_need_origin(tmp_path):
    with refining(tmp_path) as r:
        a = application(r)
        cid = company_of(r)
        contact = insert_contact(db(r), "contact-sam", cid, "Sam Fictional")
        link_contact(db(r), a.id, contact)
        routes = [
            (f"/companies/{cid}/contacts", contact_values()),
            (f"/companies/{cid}/contacts/{contact}", {**contact_values(), "version": "1"}),
            (f"/applications/{a.id}/contacts", {"contact_id": contact}),
            (f"/applications/{a.id}/contacts/new", contact_values(name="New Fictional")),
            (f"/applications/{a.id}/contacts/{contact}/unlink", {}),
            (f"/applications/{a.id}/interactions", interaction_values(contact_id=contact)),
        ]
        before = snapshot(r)
        for path, data in routes:
            assert r.client.post(path, data=data).status_code == 403, path
            origin_less = r.client.post(path, data=data, headers=r.run.run.with_token())
            assert origin_less.status_code == 403, path
            assert post(r, path, **data, extra="bad").status_code == 400, path
            assert snapshot(r) == before, path
        # With the token and the origin the first of them works, so the 403s were about those.
        assert post(r, routes[0][0], **routes[0][1]).status_code == 200


def test_contact_and_interaction_text_escaped(tmp_path):
    attack = {
        "name": "<script>fictional()</script>",
        "role": "<img src=x onerror=fictional()>",
        "relationship": '"><b>bold</b> {{ 7 * 7 }}',
        "notes": '<iframe src="javascript:fictional()"></iframe> & more',
        "phone": "<u>555</u>",
    }
    with refining(tmp_path) as r:
        a = application(r)
        cid = company_of(r)
        added = post(r, f"/companies/{cid}/contacts", **contact_values(**attack))
        assert added.status_code == 200
        [row] = contacts(r)
        assert row["name"] == attack["name"]  # stored as typed, escaped only when shown
        # A stored email that is not an address is text, never a link.
        stored = insert_contact(
            db(r), "contact-stored", cid, "Stored Fictional", email="javascript:fictional()"
        )
        link_contact(db(r), a.id, row["id"])
        link_contact(db(r), a.id, stored)
        note = "<script>interaction()</script> & <b>bold</b>"
        logged = post(r, f"/applications/{a.id}/interactions", **interaction_values(note=note))
        assert logged.status_code == 200
        pages = {
            "company": company_page(r, cid),
            "drawer": detail(r, a.id),
            "page": r.client.get(f"/applications/{a.id}").text,
            "added": added.text,
            "logged": logged.text,
        }
        for label, page in pages.items():
            assert "<script>fictional()" not in page and "<script>interaction()" not in page, label
            assert "<iframe" not in page and "<img src=x" not in page, label
            assert "<b>bold</b>" not in page and "<u>555</u>" not in page, label
            assert not [e for e in elements(page) if "onerror" in e.attrs], label
            assert all(not h.lower().startswith("javascript:") for h in hrefs_all(page)), label
            if label in ("company", "added"):  # where the relationship is shown
                assert "{{ 7 * 7 }}" in page, label
        assert "&lt;script&gt;fictional()" in pages["company"]
        assert "&lt;script&gt;interaction()" in pages["drawer"]
        assert mailto_hrefs(pages["company"]).count("mailto:sam@example.test") == 1
        assert not any("javascript" in h for h in mailto_hrefs(pages["company"]))


def hrefs_all(page):
    return [str(e.attrs["href"]) for e in elements(page) if e.tag == "a" and "href" in e.attrs]


# --- the company page needs no token --------------------------------------------------------------


def test_company_page_shows_contacts_and_history_without_token(tmp_path):
    with refining(tmp_path) as r:
        first, second, third, _ = seed_history(r)
        cid = company_of(r)
        insert_contact(db(r), "contact-b", cid, "bea Fictional", role="Recruiter")
        insert_contact(
            db(r),
            "contact-a",
            cid,
            "Avery Fictional",
            role="Hiring manager",
            email="avery@example.test",
            phone="+00 555 0111",
            relationship="Former colleague",
            notes="Likes short messages.",
        )
        insert_contact(db(r), "contact-c", cid, "Cam Fictional", email="not-an-email")
        link_contact(db(r), second.id, "contact-a")
        insert_interaction(
            db(r), "inter-1", contact_id="contact-a", application_id=second.id, day="2026-10-12"
        )
        seeder(r).job("quiet-job", "Boiler Fitter", "Dune Fictional Ltd")
        quiet_id = company_of(r, "Dune Fictional Ltd")
        at_clock(r)("2026-10-21T09:00:00+00:00")
        before = snapshot(r)

        for method in (r.client.get, r.client.head):
            answer = method(f"/companies/{cid}")
            assert answer.status_code == 200
        page = r.client.get(f"/companies/{cid}").text
        assert (
            "<html" in page and 'id="company-contacts"' in page and 'id="company-history"' in page
        )
        assert by_id(page, "company-contacts") is not None
        assert by_id(page, "company-history") is not None
        headings = {e.text for e in elements(page) if e.tag == "h2"}
        assert {"Contacts", "History"} <= headings

        items = contact_items(page)
        assert list(items) == ["contact-a", "contact-b", "contact-c"]  # by name, not by case
        avery = items["contact-a"]
        for text in (
            "Avery Fictional",
            "Hiring manager",
            "avery@example.test",
            "+00 555 0111",
            "Former colleague",
            "Likes short messages.",
            "Last contact: 2026-10-12",
        ):
            assert text in avery.text, text
        assert f"/applications/{second.id}" in hrefs_in(page, avery)
        assert f"/applications/{first.id}" not in hrefs_in(page, avery)
        assert hrefs_in(page, avery).count("mailto:avery@example.test") == 1
        # Only a valid shape is a mailto link.
        assert mailto_hrefs(page) == ["mailto:avery@example.test"]
        assert "not-an-email" in items["contact-c"].text
        # Each contact has an edit form and the section an add form (the page itself needs no
        # token; the forms' requests carry it).
        actions = form_actions(page)
        assert f"/companies/{cid}/contacts" in actions
        for contact_id in items:
            assert f"/companies/{cid}/contacts/{contact_id}" in actions
        assert set(history_items(page)) == {first.id, second.id, third.id}

        # A company with nothing yet says so and still offers the add form.
        quiet = r.client.get(f"/companies/{quiet_id}").text
        assert "No contacts yet" in quiet and "No applications yet" in quiet
        assert not contact_items(quiet) and not history_items(quiet)
        assert f"/companies/{quiet_id}/contacts" in form_actions(quiet)
        assert snapshot(r) == before  # reading changed nothing


# --- an application whose job has no company ------------------------------------------------------


def test_application_without_a_company_cannot_have_contacts(tmp_path):
    with refining(tmp_path) as r:
        a = application(r, "no-company-job", "Seed Buyer", None)
        shown = detail(r, a.id)
        box = by_id(shown, "application-contacts")
        assert box is not None and "no company yet" in box.text
        assert not contact_items(shown, "application-contacts")
        assert f"/applications/{a.id}/contacts/new" not in form_actions(shown)
        assert f"/applications/{a.id}/contacts" not in form_actions(shown)
        # The interaction form still works, with no contact to pick and a hint saying why.
        assert option_values(shown, "contact_id", within="interaction-form") == [""]
        form = by_id(shown, "interaction-form")
        assert form is not None and "no company" in form.text.lower()

        before = snapshot(r)
        refused = post(r, f"/applications/{a.id}/contacts/new", **contact_values())
        assert refused.status_code == 422
        assert "no company yet" in tied_error(refused.text, "name")
        assert snapshot(r) == before
        assert post(r, f"/applications/{a.id}/contacts", contact_id="contact-x").status_code == 422
        assert snapshot(r) == before
        logged = post(r, f"/applications/{a.id}/interactions", **interaction_values())
        assert logged.status_code == 200 and len(interactions(r)) == 1
        assert interactions(r)[0]["contact_id"] is None


def test_unknown_or_unsafe_ids_on_the_application_contact_routes(tmp_path):
    with refining(tmp_path) as r:
        a = application(r)
        contact = insert_contact(db(r), "contact-sam", company_of(r), "Sam Fictional")
        before = snapshot(r)
        for aid in ("missing", "bad.id", "x" * 65):
            routes = [
                (f"/applications/{aid}/contacts", {"contact_id": contact}),
                (f"/applications/{aid}/contacts/new", contact_values()),
                (f"/applications/{aid}/contacts/{contact}/unlink", {}),
                (f"/applications/{aid}/interactions", interaction_values()),
            ]
            for path, data in routes:
                answer = post(r, path, **data)
                assert answer.status_code == 404, path
                assert answer.text == ApplicationNotFound.message and aid not in answer.text
        assert post(r, f"/applications/{a.id}/contacts", contact_id="x" * 65).status_code == 422
        assert snapshot(r) == before


# --- the guards of migration 0005 -----------------------------------------------------------------


def undo(r, job_id, route="triage"):
    version = r.run.rows("SELECT version FROM jobs WHERE id=?", (job_id,))[0]["version"]
    return post(r, f"/inbox/jobs/{job_id}/{route}", version=str(version), action="undo")


def test_undo_and_back_to_queue_are_refused_after_linking_a_contact(tmp_path):
    with refining(tmp_path) as r:
        for job, route in (("undo-card", "triage"), ("undo-drawer", "detail-triage")):
            a = application(r, job, "Seed Buyer", LARKSPUR, "preparing")
            contact = insert_contact(db(r), f"contact-{job}", company_of(r), f"Sam {job}")
            assert post(r, f"/applications/{a.id}/contacts", contact_id=contact).status_code == 200
            before = snapshot(r)
            refused = undo(r, job, route)
            assert refused.status_code == 409 and UNDO_REFUSED in refused.text, route
            assert snapshot(r) == before
            assert (a.id, contact) in links(r)
            assert r.run.rows("SELECT id FROM applications WHERE id=?", (a.id,))


def test_undo_and_back_to_queue_are_refused_after_logging_an_interaction(tmp_path):
    with refining(tmp_path) as r:
        for job, route in (("undo-card", "triage"), ("undo-drawer", "detail-triage")):
            a = application(r, job, "Seed Buyer", LARKSPUR, "preparing")
            contact = insert_contact(db(r), f"contact-{job}", company_of(r), f"Sam {job}")
            logged = post(
                r, f"/applications/{a.id}/interactions", **interaction_values(contact_id=contact)
            )
            assert logged.status_code == 200
            before = snapshot(r)
            refused = undo(r, job, route)
            assert refused.status_code == 409 and UNDO_REFUSED in refused.text, route
            assert snapshot(r) == before
            assert r.run.rows("SELECT id FROM applications WHERE id=?", (a.id,))
            assert contact in {c["id"] for c in contacts(r)}


def job_row(r, job_id):
    return r.run.rows("SELECT * FROM jobs WHERE id=?", (job_id,))[0]


@pytest.mark.parametrize("how", ["link", "interaction"])
def test_changing_a_jobs_company_is_refused_while_contacts_are_tied_to_it(tmp_path, how):
    """The migration keeps a linked contact's company; the editor says so in words (ADR-105)."""
    with refining(tmp_path) as r:
        a = application(r, "edit-job", "Seed Buyer", LARKSPUR)
        contact = insert_contact(db(r), "contact-sam", company_of(r), "Sam Fictional")
        if how == "link":
            link_contact(db(r), a.id, contact)
        else:  # only an interaction ties the contact to the application
            insert_interaction(
                db(r), "inter-1", contact_id=contact, application_id=a.id, day="2026-10-12"
            )
            assert links(r) == []
        edit = "/inbox/jobs/edit-job/edit"
        version = str(job_row(r, "edit-job")["version"])
        before = snapshot(r)
        refused = post(r, edit, version=version, company="Another Fictional Ltd")
        assert refused.status_code == 422
        assert 'aria-describedby="job-edit-company-error"' in refused.text
        assert "unlink" in refused.text.lower()
        assert snapshot(r) == before  # no new company either

        # The same company, or another field, is still saved.
        same = post(r, edit, version=version, company=LARKSPUR, title="New Title")
        assert same.status_code == 200 and job_row(r, "edit-job")["title"] == "New Title"

        if how == "link":  # without the tie, the company can change
            assert post(r, f"/applications/{a.id}/contacts/{contact}/unlink").status_code == 200
            version = str(job_row(r, "edit-job")["version"])
            moved = post(r, edit, version=version, company="Another Fictional Ltd")
            assert moved.status_code == 200
            assert r.run.rows("SELECT name FROM companies WHERE name='Another Fictional Ltd'")


def test_resummary_keeps_the_company_of_a_job_whose_application_has_contacts(tmp_path):
    """A new summary may name another company; it must not fail on the migration guard."""
    with importing(tmp_path) as run:
        summary_answer(run)
        job_id = summary_seed(run)
        summary_start(run, job_id)
        before = run.rows("SELECT company_id, version FROM jobs WHERE id=?", (job_id,))[0]
        applied = run.app.state.applications.apply_to_job(
            job_id, expected_job_version=before["version"]
        )
        database = run.app.state.workspace.db
        insert_contact(database, "contact-sam", before["company_id"], "Sam Fictional")
        link_contact(database, applied.application.id, "contact-sam")

        summary_answer(run, {**SUMMARY_OUTPUT, "company": "Other Fictional Ltd"})
        summary_start(run, job_id)  # succeeds
        after = run.rows("SELECT company_id FROM jobs WHERE id=?", (job_id,))[0]
        assert after["company_id"] == before["company_id"]


# --- names, focus and wording ---------------------------------------------------------------------


def forms_by_id(page):
    return {e.attrs["id"]: e for e in elements(page) if e.tag == "form" and "id" in e.attrs}


def test_every_contact_form_has_an_accessible_name(tmp_path):
    with refining(tmp_path) as r:
        a = application(r)
        cid = company_of(r)
        sam = insert_contact(db(r), "contact-sam", cid, "Sam Fictional")
        page = company_page(r, cid)
        forms = forms_by_id(page)
        assert forms["contact-form-new"].attrs["aria-labelledby"] == "company-contact-add-heading"
        assert by_id(page, "company-contact-add-heading") is not None
        assert forms[f"contact-form-{sam}"].attrs["aria-label"] == "Edit Sam Fictional"
        shown = detail(r, a.id)
        forms = forms_by_id(shown)
        assert forms["application-contact-form"].attrs["aria-labelledby"] == (
            "application-contact-add-heading"
        )
        assert forms["interaction-form"].attrs["aria-labelledby"] == "log-interaction-heading"
        assert forms["contact-link-form"].attrs["aria-label"] == "Link a contact"
        for heading in ("application-contact-add-heading", "log-interaction-heading"):
            assert by_id(shown, heading) is not None


def test_drawer_focus_goes_to_the_contacts_heading_for_contact_changes(tmp_path):
    with refining(tmp_path) as r:
        a = application(r)
        sam = insert_contact(db(r), "contact-sam", company_of(r), "Sam Fictional")

        def focused(answer):
            return [
                str(e.attrs["id"]) for e in elements(answer.text) if "data-autofocus" in e.attrs
            ]

        assert focused(post(r, f"/applications/{a.id}/contacts", contact_id=sam)) == [
            "application-contacts-heading"
        ]
        assert focused(post(r, f"/applications/{a.id}/contacts/{sam}/unlink")) == [
            "application-contacts-heading"
        ]
        added = post(r, f"/applications/{a.id}/contacts/new", **contact_values(name="Pat"))
        assert focused(added) == ["application-contacts-heading"]
        logged = post(r, f"/applications/{a.id}/interactions", **interaction_values())
        assert focused(logged) == ["application-title"]
        # The headings can take focus.
        heading = by_id(added.text, "application-contacts-heading")
        assert heading is not None and heading.attrs["tabindex"] == "-1"


def test_wording_of_the_company_page_and_the_interaction_form(tmp_path):
    with refining(tmp_path) as r:
        a = application(r)
        cid = company_of(r)
        quiet = company_page(r, cid)
        assert "No contacts yet" in by_id(quiet, "company-contacts").text
        added = post(r, f"/companies/{cid}/contacts", **contact_values())
        assert added.status_code == 200
        message = [e for e in elements(added.text) if e.text == "Contact saved."]
        assert message and all("role" not in e.attrs for e in message)  # announced instead
        assert "Status: Applied" in by_id(company_page(r, cid), "company-history").text
        assert "Interactions can't be edited yet" in by_id(detail(r, a.id), "interaction-form").text
        # The limits shown in the forms come from the domain.
        assert 'maxlength="254"' in quiet and 'maxlength="10000"' in quiet
