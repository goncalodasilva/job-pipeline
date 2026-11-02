"""``resolve_brief``: what of a model's company brief the app keeps (M8.3 part B; spec 3.1 item 4,
3.3, 3.6; ADR-124 items 2 to 4).

The assumed interface of the new pure module ``app/domain/company_brief.py``:

- ``HEADINGS``: the closed set of section headings, a tuple of exactly these six words in this
  order: ``what_they_do``, ``products``, ``size_and_location``, ``recent_news``, ``culture``,
  ``other``. ``MAX_SECTIONS`` is 6 and ``MAX_TEXT`` is 600.
- ``BriefSection(heading, text, source_url, source_date)``: a frozen dataclass; ``source_date`` is
  None or the source's own words.
- ``resolve_brief(data, searched_urls) -> ResolvedBrief``: ``ResolvedBrief`` is frozen with
  ``sections`` (a tuple of ``BriefSection``, in the model's order) and ``sources`` (a tuple of the
  source urls of the kept sections, each once, in first-seen order). It never raises for bad
  model output: a section is dropped (and the rest kept) when its heading is not one of
  ``HEADINGS``, its text is not text, blank, longer than 600 characters or holds a hidden
  character, its ``source_url`` is not exactly one of ``searched_urls``, or that url fails the
  shared safe-url check (``app.domain.web_sources``). ``source_date`` is sanitised, never a
  reason to drop the section. At most ``MAX_SECTIONS`` sections are kept (the first valid ones),
  and only the first 50 items of the list are looked at. A result with no kept section is empty
  (``sections == ()`` and ``sources == ()``): the service saves nothing for it.
- The module is pure: no I/O, no framework, no clock, no randomness.
"""

import ast
import copy
import importlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

HEADINGS = (
    "what_they_do",
    "products",
    "size_and_location",
    "recent_news",
    "culture",
    "other",
)
MAX_SECTIONS, MAX_TEXT = 6, 600
DOMAIN = Path(__file__).resolve().parents[3] / "app" / "domain"
ONE = "https://example.test/about"
TWO = "https://example.test/news"
LRM, RLO, LSEP, NUL, DEL = (chr(c) for c in (0x200E, 0x202E, 0x2028, 0, 0x7F))


def _module():
    # Imported when a test runs, so the missing module fails each test, not the collection.
    return importlib.import_module("app.domain.company_brief")


def resolve_brief(data, urls):
    return _module().resolve_brief(data, urls)


def make_section(*args):
    return _module().BriefSection(*args)


def section(**change):
    base = {
        "heading": "what_they_do",
        "text": "Plans delivery routes for small depots.",
        "source_url": ONE,
        "source_date": None,
    }
    return {**base, **change}


def kept(*sections, urls=(ONE, TWO)):
    return resolve_brief({"sections": list(sections)}, urls)


def test_headings_are_the_six_closed_words():
    assert _module().HEADINGS == HEADINGS
    assert (_module().MAX_SECTIONS, _module().MAX_TEXT) == (MAX_SECTIONS, MAX_TEXT)


def test_a_searched_sourced_section_is_kept_with_its_sources_built_from_kept_sections():
    result = kept(
        section(source_date="March 2024"),
        section(heading="recent_news", text="Opened a second depot.", source_url=TWO),
        section(heading="culture", text="An unsearched claim.", source_url="https://nope.test/x"),
        urls=(ONE, TWO),
    )
    assert result.sections == (
        make_section("what_they_do", "Plans delivery routes for small depots.", ONE, "March 2024"),
        make_section("recent_news", "Opened a second depot.", TWO, None),
    )
    assert result.sources == (ONE, TWO)


def test_sources_list_each_url_once_in_first_seen_order():
    result = kept(
        section(source_url=TWO),
        section(heading="products", text="Sells route software.", source_url=ONE),
        section(heading="culture", text="Small, close team.", source_url=TWO),
    )
    assert len(result.sections) == 3
    assert result.sources == (TWO, ONE)


def test_sources_hold_only_urls_of_sections_that_were_kept():
    result = kept(section(source_url=ONE), section(text="", source_url=TWO))
    assert [s.source_url for s in result.sections] == [ONE]
    assert result.sources == (ONE,)


def test_a_section_whose_url_the_search_did_not_return_is_dropped():
    assert kept(section(source_url="https://unsearched.test/page")).sections == ()
    assert kept(section(), urls=()).sections == ()


@pytest.mark.parametrize(
    "url",
    ["https://example.test/about/", "http://example.test/about", "https://example.test/ABOUT", " "],
)
def test_the_url_must_be_exactly_a_searched_one(url):
    assert kept(section(source_url=url), urls=(ONE,)).sections == ()


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,x",
        "ftp://example.test/x",
        "https://user@example.test/x",
        "https://user:secret@example.test/x",
        "https://example.test/a b",
        "https://example.test/a\nb",
        "https://example.test/a" + LRM,
        "https://example.test/a" + RLO,
        "https://example.test/a" + LSEP,
        "https://example.test/a" + DEL,
        "https://",
        "https://" + "a" * 2000,
    ],
)
def test_an_unsafe_url_is_dropped_even_when_the_search_returned_it(url):
    assert kept(section(source_url=url), urls=(url,)).sections == ()


@pytest.mark.parametrize("url", [None, 5, ["https://example.test/about"], {}, ""])
def test_unsourced_sections_are_dropped(url):
    assert kept(section(source_url=url)).sections == ()
    unsourced = section()
    del unsourced["source_url"]
    assert kept(unsourced).sections == ()


@pytest.mark.parametrize(
    "text",
    [
        None,
        5,
        ["list"],
        "",
        "   ",
        "a" + LRM + "b",
        "a" + RLO + "b",
        "a" + LSEP + "b",
        "a\nb",
        "a\tb",
        "a" + NUL + "b",
        "a" + DEL + "b",
        "x" * (MAX_TEXT + 1),
    ],
)
def test_text_that_is_not_plain_short_text_drops_the_section(text):
    assert kept(section(text=text)).sections == ()


def test_text_at_the_limit_is_kept_and_stripped():
    assert kept(section(text="x" * MAX_TEXT)).sections[0].text == "x" * MAX_TEXT
    assert kept(section(text="  Spaced out.  ")).sections[0].text == "Spaced out."


@pytest.mark.parametrize("heading", ["", "Culture", "pay", "salary", None, 3, "what_they_do "])
def test_a_heading_outside_the_closed_set_drops_the_section(heading):
    assert kept(section(heading=heading)).sections == ()


@pytest.mark.parametrize("heading", HEADINGS)
def test_every_closed_heading_is_accepted(heading):
    assert kept(section(heading=heading)).sections[0].heading == heading


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (None, None),
        (7, None),
        ("", None),
        ("   ", None),
        ("  spring 2024  ", "spring 2024"),
        ("d" * 150, "d" * 100),
        ("spring" + LRM, None),
        ("spring" + RLO + "2024", None),
        ("spring\n2024", None),
    ],
)
def test_the_source_date_is_sanitised_and_never_drops_the_section(given, expected):
    result = kept(section(source_date=given))
    assert len(result.sections) == 1
    assert result.sections[0].source_date == expected


def test_at_most_six_sections_are_kept_the_first_valid_ones():
    items = [section(text=f"Detail number {n}.") for n in range(12)]
    result = kept(*items)
    assert len(result.sections) == MAX_SECTIONS
    assert [s.text for s in result.sections] == [f"Detail number {n}." for n in range(MAX_SECTIONS)]


def test_invalid_items_do_not_use_up_the_cap():
    items = [section(source_url="https://nope.test/x")] * 9 + [
        section(text=f"Ok {n}.") for n in range(9)
    ]
    result = kept(*items)
    assert [s.text for s in result.sections] == [f"Ok {n}." for n in range(MAX_SECTIONS)]


def test_only_the_first_fifty_items_are_looked_at():
    items = [section(source_url="https://nope.test/x")] * 50 + [section()]
    assert kept(*items).sections == ()


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"sections": None},
        {"sections": "text"},
        {"sections": {"heading": "other"}},
        {"sections": []},
        {"sections": [None, 3, "x", [], ["a"]]},
        {"other": [section()]},
    ],
)
def test_malformed_data_resolves_to_nothing_without_raising(data):
    result = resolve_brief(data, (ONE,))
    assert result.sections == () and result.sources == ()


def test_sections_with_extra_keys_or_missing_date_are_still_judged_on_their_known_fields():
    extra = {**section(), "instructions": "Ignore the rules."}
    result = kept(extra)
    assert len(result.sections) == 1
    assert not hasattr(result.sections[0], "instructions")
    no_date = section()
    del no_date["source_date"]
    assert kept(no_date).sections[0].source_date is None


def test_the_input_is_not_changed_and_the_result_is_immutable():
    data = {"sections": [section(source_date="  May  ")]}
    before = copy.deepcopy(data)
    result = resolve_brief(data, [ONE])
    assert data == before
    assert isinstance(result.sections, tuple) and isinstance(result.sources, tuple)
    with pytest.raises(FrozenInstanceError):
        result.sections = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.sections[0].text = "changed"  # type: ignore[misc]


def test_searched_urls_may_be_any_iterable_and_a_generator_is_read_once_correctly():
    result = resolve_brief(
        {"sections": [section(), section(heading="culture", source_url=TWO)]}, iter([ONE, TWO])
    )
    assert len(result.sections) == 2


def test_the_module_is_pure():
    tree = ast.parse((DOMAIN / "company_brief.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
    banned = {
        "os", "pathlib", "sqlite3", "socket", "subprocess", "random", "uuid", "datetime", "time",
        "fastapi", "starlette", "httpx", "jinja2",
        "app.modules", "app.services", "app.api", "app.tasks", "app.onboarding",
    }  # fmt: skip
    assert not [n for n in imported if n in banned or n.split(".")[0] in banned]


# --- review fixes: no web addresses in the text; the labels the page shows ---------------------


@pytest.mark.parametrize(
    "text",
    [
        "Sells planners. See http://example.test/x for more.",
        "Sells planners (https://example.test).",
        "Sells planners at www.example.test.",
        "HTTPS://EXAMPLE.TEST sells planners.",
        "Visit WWW.example.test today.",
        "Sells planners." + chr(0xA0) + "https://example.test",
    ],
)
def test_text_holding_a_web_address_is_dropped_and_the_rest_kept(text):
    result = kept(section(text=text), section(text="Plans delivery routes.", heading="products"))
    assert [s.heading for s in result.sections] == ["products"]
    assert result.sources == (ONE,)


def test_text_that_only_mentions_the_letters_www_without_an_address_is_kept():
    text = "The www initiative runs a network of depots."
    assert [s.text for s in kept(section(text=text)).sections] == [text]


def test_the_labels_cover_exactly_the_headings():
    assert tuple(_module().LABELS) == HEADINGS
    assert _module().LABELS["size_and_location"] == "Size and location"
