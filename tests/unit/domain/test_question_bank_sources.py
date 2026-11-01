"""Source and wording checks for web-found questions (M8.1 review)."""

import pytest

from app.domain.question_bank import resolve_web_items

URL = "https://example.test/interviews"
# Code points are spelled out so editors and formatters can't turn them into invisible text.
DEL, NEL, LRM, RLM, ALM = (chr(c) for c in (0x7F, 0x85, 0x200E, 0x200F, 0x061C))
RLO, RLI, NBSP, LSEP, NUL = (chr(c) for c in (0x202E, 0x2067, 0xA0, 0x2028, 0))


def item(text="Describe a fictional challenge.", url=URL, date="2025"):
    return {"text": text, "type": "behavioral", "source_url": url, "source_date": date}


def kept(data, urls=(URL,)):
    return resolve_web_items({"questions": [data]}, urls)


@pytest.mark.parametrize(
    "url",
    [
        "http://:secret@example.test/x",
        "https://user@example.test/x",
        "https://example.test/x" + DEL,
        "https://example.test/x" + NEL + "y",
        "https://example.test/x" + RLO + "y",
        "https://example.test/x" + RLM + "y",
        "https://example.test/x" + NBSP + "y",
        "https://example.test/x" + LSEP + "y",
    ],
)
def test_unsafe_source_urls_dropped_even_when_searched(url):
    assert kept(item(url=url), urls=(url,)) == ()


@pytest.mark.parametrize("mark", [LRM, RLM, ALM, RLO, RLI, NUL, "\n"])
def test_question_text_with_control_or_bidi_marks_dropped(mark):
    assert kept(item(text=f"Describe{mark} a challenge.")) == ()


@pytest.mark.parametrize("date", ["2025" + NUL, "spring" + RLO, "a\nb", "x" + RLM + "y"])
def test_source_date_with_control_or_bidi_marks_becomes_none(date):
    (draft,) = kept(item(date=date))
    assert draft.source_url == URL and draft.source_date is None


def test_plain_source_date_kept():
    (draft,) = kept(item(date="  last spring "))
    assert draft.source_date == "last spring"
