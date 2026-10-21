"""M5.6: contact and interaction checks, email shape, history wording and the 30-day count."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain.company_history import (
    ordered_history,
    reapply_note,
    recent_applications,
    status_words,
)
from app.domain.contacts import (
    ContactInvalid,
    checked_contact,
    checked_interaction,
    interaction_instant,
    mailto_link,
    valid_email_shape,
)


@pytest.mark.parametrize(
    "email",
    ["sam@example.test", "sam.o+tag@mail.example.test", "o'brien@example.test", "ana@exämple.test"],
)
def test_email_shapes_that_are_valid(email):
    assert valid_email_shape(email) and mailto_link(email) == f"mailto:{email}"


@pytest.mark.parametrize(
    "email",
    [
        "",
        "plain",
        "nobody@",
        "@example.test",
        "two@@example.test",
        "a@b",
        "spaces in@example.test",
        "a..b@example.test",
        ".a@example.test",
        "a?bcc=x@example.test",
        "javascript:fictional()",
        "<b>@example.test",
        "a@exa mple.test",
        "a@-example.test",
        "a@example..test",
        "a@" + "x" * 250 + ".test",
    ],
)
def test_email_shapes_that_are_not(email):
    assert not valid_email_shape(email) and mailto_link(email) is None


def test_contact_fields_are_trimmed_and_the_name_is_required():
    assert checked_contact({"name": "  Sam ", "notes": "a\r\nb"}) == {
        "name": "Sam",
        "role": "",
        "email": "",
        "phone": "",
        "relationship": "",
        "notes": "a\nb",
    }
    with pytest.raises(ContactInvalid) as error:
        checked_contact({"name": " ", "email": "bad", "role": "x" * 201, "phone": "1\u200b2"})
    assert set(error.value.errors) == {"name", "email", "role", "phone"}


def test_interaction_needs_a_real_day_a_known_channel_and_a_bounded_note():
    assert (
        checked_interaction({"date": "2026-10-18", "channel": "call", "note": " hi "}).note == "hi"
    )
    with pytest.raises(ContactInvalid) as error:
        checked_interaction({"date": "2026-02-30", "channel": "pigeon", "note": "x" * 10001})
    assert set(error.value.errors) == {"date", "channel", "note"}
    assert interaction_instant(date(2026, 10, 18)) == datetime(2026, 10, 18, 12, tzinfo=UTC)


def test_history_wording_and_the_fixed_30_day_window():
    assert status_words("Rejected", "closed") == "Closed: rejected"
    assert status_words("Interviewing", "active") == "Interviewing"
    now = datetime(2026, 11, 10, 12, tzinfo=UTC)
    applied = [now - timedelta(days=29), now - timedelta(days=30), now - timedelta(days=31), None]
    assert recent_applications(applied, now) == 2
    assert reapply_note(1) == "" and reapply_note(0) == ""
    assert reapply_note(2) == "2 applications here in the last 30 days."


def test_zero_width_joiners_are_allowed_but_other_invisible_characters_are_not():
    joined = "\u0915\u094d\u200d\u0937"  # a conjunct that needs a joiner
    assert checked_contact({"name": joined})["name"] == joined
    for invisible in ("\u200b", "\u202e", "\ufeff", "\u2028"):
        with pytest.raises(ContactInvalid) as error:
            checked_contact({"name": f"Sa{invisible}m", "relationship": f"a{invisible}b"})
        assert set(error.value.errors) == {"name", "relationship"}


def test_newlines_and_tabs_are_fine_in_notes_but_control_characters_never_are():
    assert checked_contact({"name": "Sam", "notes": "a\tb\nc"})["notes"] == "a\tb\nc"
    with pytest.raises(ContactInvalid) as error:
        checked_contact({"name": "Sam\nLee", "role": "a\tb", "notes": "x\x00y"})
    assert set(error.value.errors) == {"name", "role", "notes"}
    with pytest.raises(ContactInvalid) as error:
        checked_contact({"name": "Sam", "notes": "bell\x07"})
    assert set(error.value.errors) == {"notes"}
    with pytest.raises(ContactInvalid) as error:
        checked_interaction({"date": "2026-10-18", "channel": "call", "note": "a\x00b"})
    assert set(error.value.errors) == {"note"}
    assert (
        checked_interaction({"date": "2026-10-18", "channel": "call", "note": "a\nb"}).note
        == "a\nb"
    )


def test_status_words_keep_acronyms_and_names():
    assert status_words("NDA signed", "closed") == "Closed: NDA signed"
    assert status_words("Offer declined", "closed") == "Closed: offer declined"
    assert status_words("X", "closed") == "Closed: x"
    assert status_words("NDA signed", "active") == "NDA signed"


def test_history_lists_applied_first_newest_first_then_the_ones_not_yet_applied():
    rows = [
        {
            "id": "a",
            "applied_at": "2026-09-01T12:00:00+00:00",
            "created_at": "2026-09-01T11:00:00+00:00",
        },
        {"id": "b", "applied_at": None, "created_at": "2026-10-06T12:00:00+00:00"},
        {
            "id": "c",
            "applied_at": "2026-10-05T12:00:00+00:00",
            "created_at": "2026-10-05T11:00:00+00:00",
        },
        {"id": "d", "applied_at": None, "created_at": "2026-10-07T12:00:00+00:00"},
    ]
    assert [row["id"] for row in ordered_history(rows)] == ["c", "a", "d", "b"]
