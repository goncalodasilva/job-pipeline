"""Page text (M4.1; spec 2.4, 3.3; ADR-098 item 2).

``page_text(page: FetchedPage) -> str`` lives in ``app.modules.fetch.text`` (stdlib
``html.parser`` and ``json``) and is exported from the provider-free interface
``app.modules.fetch``. It drops script, style, nav and hidden parts (the ``hidden``
attribute, ``aria-hidden="true"``, inline ``display:none`` or ``visibility:hidden``), keeps
headings, paragraphs and list items as separate lines (list items start with "- "),
collapses whitespace and unescapes entities. A ``JobPosting`` in JSON-LD with a
description is preferred over the page body. ``text/plain`` pages come back normalized. A
JavaScript-only shell has no job text.

Every page is fictional. The new name is imported inside the tests.
"""

import importlib
import json
import re
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from app.modules.fetch import FetchedPage
from tests.job_pages import page_bytes, page_entries, page_entry, page_url

URL = "https://jobs.lumenboard.example.test/postings/1"


def _page_text() -> Any:
    fetch = importlib.import_module("app.modules.fetch")
    assert hasattr(fetch, "page_text"), "app.modules.fetch.page_text doesn't exist yet"
    text_module = importlib.import_module("app.modules.fetch.text")
    assert fetch.page_text is text_module.page_text
    return fetch.page_text


def _page(text: str, content_type: str = "text/html") -> FetchedPage:
    return FetchedPage(
        requested_url=URL, final_url=URL, status=200, content_type=content_type, text=text
    )


def _fixture_page(page_id: str) -> FetchedPage:
    entry = page_entry(page_id)
    charset = "iso-8859-1" if "iso-8859-1" in entry["content_type"] else "utf-8"
    url = page_url(page_id)
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status=200,
        content_type="text/html",
        text=page_bytes(page_id).decode(charset),
    )


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def test_page_text_keeps_headings_paragraphs_lists() -> None:
    page_text = _page_text()
    html = """
    <html><head><title>Ignored title</title></head><body>
      <h1>Senior   Widget
          Engineer</h1>
      <p>Join <strong>Northwind</strong> Fictional Labs. R&amp;D &eacute;quipe &#8211; Lisbon.</p>
      <h2>Requirements</h2>
      <ul><li>Five years of Python</li><li>Experience   with <em>SQLite</em></li></ul>
      <ol><li>Apply online</li></ol>
      <div>Hybrid<br>Full-time</div>
      <p>Salary 60,000 EUR</p>
    </body></html>
    """

    lines = _lines(page_text(_page(html)))

    for expected in (
        "Senior Widget Engineer",
        "Join Northwind Fictional Labs. R&D \u00e9quipe \u2013 Lisbon.",
        "Requirements",
        "- Five years of Python",
        "- Experience with SQLite",
        "- Apply online",
        "Salary 60,000 EUR",
    ):
        assert expected in lines, (expected, lines)
    assert lines.index("Senior Widget Engineer") < lines.index("Requirements")
    assert lines.index("- Five years of Python") < lines.index("- Experience with SQLite")
    # Blocks are separate lines, never run together.
    assert not any("Requirements- " in line or "EngineerJoin" in line for line in lines)

    # Every fixture keeps its markers.
    for page_id, entry in page_entries().items():
        text = page_text(_fixture_page(page_id))
        flat = re.sub(r"\s+", " ", text)
        for marker in entry["markers"]:
            assert marker in flat, (page_id, marker)

    # Plain text comes back as text: lines kept, runs of spaces collapsed, nothing parsed.
    plain = page_text(_page("Data   Gardener\n\nQuill Orchard <Fictional>\n", "text/plain"))
    assert _lines(plain) == ["Data Gardener", "Quill Orchard <Fictional>"]


def test_page_text_drops_script_style_nav_hidden() -> None:
    page_text = _page_text()
    html = """
    <html><head>
      <style>.secret-style { color: red; }</style>
      <script>var secretScript = "tracker";</script>
    </head><body>
      <nav><a href="/">Board home NAVTEXT</a><ul><li>NAVITEM</li></ul></nav>
      <p>Visible one.</p>
      <div hidden><p>HIDDEN-ATTR</p><img src="x.png"><br><p>HIDDEN-AFTER-VOID</p></div>
      <p>Visible two.</p>
      <div aria-hidden="true">ARIA-HIDDEN</div>
      <span style="display:none">DISPLAY-NONE</span>
      <span style="color: blue; DISPLAY: None ;">DISPLAY-NONE-SPACED</span>
      <span style="visibility: hidden">VISIBILITY-HIDDEN</span>
      <div aria-hidden="false">Visible three.</div>
      <script type="text/javascript">document.write("SCRIPT-BODY")</script>
      <noscript><style>.x{}</style></noscript>
      <p>Visible four.</p>
    </body></html>
    """

    text = page_text(_page(html))

    for gone in (
        "secret-style",
        "secretScript",
        "NAVTEXT",
        "NAVITEM",
        "HIDDEN-ATTR",
        "HIDDEN-AFTER-VOID",
        "ARIA-HIDDEN",
        "DISPLAY-NONE",
        "VISIBILITY-HIDDEN",
        "SCRIPT-BODY",
    ):
        assert gone not in text, gone
    lines = _lines(text)
    for kept in ("Visible one.", "Visible two.", "Visible three.", "Visible four."):
        assert kept in lines, (kept, lines)

    # Implied end tags: a new list item closes a hidden one, a new paragraph closes a hidden
    # paragraph, and closing the list closes its open items.
    implied = page_text(
        _page(
            '<ul><li style="display:none">HIDDEN-ITEM<li>Python<li>SQL</ul><p>After</p>'
            "<p hidden>HIDDEN-PARAGRAPH<p>Visible requirement</p>"
            "<dl><dt hidden>HIDDEN-TERM<dt>Term<dd>Meaning</dl>"
            "<table><tr><td hidden>HIDDEN-CELL<td>Cell</table>"
            "<p hidden>HIDDEN-BEFORE-HEADING<h2>Benefits</h2>"
        )
    )
    implied_lines = _lines(implied)
    # Self-closing syntax: "<br/>" ends a line, "<div/>" is only a start tag.
    assert _lines(page_text(_page("<p>One<br/>Two</p><div/>Three"))) == ["One", "Two", "Three"]
    for kept in ("- Python", "- SQL", "After", "Visible requirement", "Term", "Meaning", "Cell"):
        assert kept in implied_lines, (kept, implied_lines)
    assert "Benefits" in implied_lines, implied_lines
    for gone in (
        "HIDDEN-ITEM",
        "HIDDEN-PARAGRAPH",
        "HIDDEN-TERM",
        "HIDDEN-CELL",
        "HIDDEN-BEFORE-HEADING",
    ):
        assert gone not in implied, gone

    # The fixtures' chrome and scripts are gone too.
    for page_id, entry in page_entries().items():
        found = page_text(_fixture_page(page_id))
        for absent in entry["absent"]:
            assert absent not in found, (page_id, absent)


DESCRIPTION = (
    "<p>Grow tidy datasets &amp; prune spreadsheets.</p><ul><li>Three years of SQL</li></ul>"
)
POSTING = {"@type": "JobPosting", "title": "Data Gardener", "description": DESCRIPTION}
JSON_LD_POSTING = json.dumps({"@context": "https://schema.org", **POSTING})
JSON_LD_GRAPH = json.dumps(
    {
        "@context": "https://schema.org",
        "@graph": [{"@type": "Organization", "name": "Quill Orchard Fictional"}, POSTING],
    }
)
JSON_LD_LIST = json.dumps(
    [
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": []},
        {"@context": "https://schema.org", **POSTING},
    ]
)


def _with_json_ld(block: str) -> str:
    return (
        "<html><head>"
        f'<script type="application/ld+json">{block}</script>'
        "</head><body><div>COOKIE-BANNER</div><p>Body text of the board.</p></body></html>"
    )


def test_json_ld_job_posting_preferred() -> None:
    page_text = _page_text()

    for block in (JSON_LD_POSTING, JSON_LD_GRAPH, JSON_LD_LIST):
        text = page_text(_page(_with_json_ld(block)))
        lines = _lines(text)
        assert "Data Gardener" in lines, (block, lines)
        assert "Grow tidy datasets & prune spreadsheets." in lines
        assert "- Three years of SQL" in lines
        assert "COOKIE-BANNER" not in text
        assert "Body text of the board." not in text
        assert "<p>" not in text and "<li>" not in text

    # The fixture: title and description from JSON-LD, none of the shell around it.
    flat = re.sub(r"\s+", " ", page_text(_fixture_page("json_ld")))
    assert "Data Gardener" in flat
    assert "Quill Orchard Fictional grows tidy datasets." in flat
    assert "We use fictional cookies" not in flat

    # No JobPosting, broken JSON or no description: the page body is used, nothing raises.
    for block in (
        '{"@type": "Organization", "name": "Quill Orchard Fictional"}',
        '{"@type": "JobPosting", "title": "Data Gardener"',
        '{"@type": "JobPosting", "title": "Data Gardener"}',
        "[1, 2, null]",
        "",
    ):
        text = page_text(_page(_with_json_ld(block)))
        assert "Body text of the board." in text, block


def test_json_ld_keeps_company_location_type_salary_and_date() -> None:
    page_text = _page_text()
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Data Gardener",
        "hiringOrganization": {"@type": "Organization", "name": "Quill Orchard Fictional"},
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Lisbon",
                "addressRegion": "Lisboa",
                "addressCountry": "PT",
            },
        },
        "employmentType": ["FULL_TIME", "CONTRACTOR"],
        "baseSalary": {
            "@type": "MonetaryAmount",
            "currency": "EUR",
            "value": {
                "@type": "QuantitativeValue",
                "minValue": 50000,
                "maxValue": 60000.0,
                "unitText": "YEAR",
            },
        },
        "datePosted": "2026-10-01",
        "description": DESCRIPTION,
    }

    lines = _lines(page_text(_page(_with_json_ld(json.dumps(posting)))))

    assert lines[0] == "Data Gardener"
    details = lines[1:6]
    for expected in ("Quill Orchard Fictional", "Lisbon, Lisboa, PT", "FULL_TIME, CONTRACTOR"):
        assert any(expected in line for line in details), (expected, lines)
    assert any("50000" in line and "60000" in line and "EUR" in line for line in details), lines
    assert any("YEAR" in line for line in details), lines
    assert any("2026-10-01" in line for line in details), lines
    assert lines[6:] == ["Grow tidy datasets & prune spreadsheets.", "- Three years of SQL"]

    # Other shapes: a list of places, a country object, a plain salary value; odd shapes are
    # skipped without raising.
    posting.update(
        {
            "jobLocation": [
                {"address": {"addressLocality": "Porto", "addressCountry": {"name": "Portugal"}}},
                {"address": "Remote, Europe"},
                "not a place",
            ],
            "baseSalary": {"currency": "EUR", "value": 45000},
            "employmentType": "PART_TIME",
            "hiringOrganization": "Quill Orchard Fictional (as text)",
        }
    )
    flat = page_text(_page(_with_json_ld(json.dumps(posting))))
    for expected in ("Porto, Portugal", "Remote, Europe", "45000", "PART_TIME"):
        assert expected in flat, (expected, flat)

    # Open-ended salary ranges; numbers that aren't finite are skipped.
    for salary, expected in (
        ({"currency": "EUR", "value": {"minValue": 40000.5}}, "from 40000.5 EUR"),
        ({"value": {"maxValue": 70000, "unitText": "YEAR"}}, "up to 70000 per YEAR"),
    ):
        text = page_text(_page(_with_json_ld(json.dumps({**POSTING, "baseSalary": salary}))))
        assert expected in text, (expected, text)
    infinite = json.dumps({**POSTING, "baseSalary": {"value": 1e400}})
    assert "Salary" not in page_text(_page(_with_json_ld(infinite)))

    odd = {
        **POSTING,
        "hiringOrganization": {"name": ["not", "a", "name"]},
        "jobLocation": {"address": {"addressLocality": 7}},
        "employmentType": {"odd": True},
        "baseSalary": {"currency": ["EUR"], "value": {"minValue": "lots"}},
        "datePosted": 20261001,
    }
    lines = _lines(page_text(_page(_with_json_ld(json.dumps(odd)))))
    assert lines == [
        "Data Gardener",
        "Grow tidy datasets & prune spreadsheets.",
        "- Three years of SQL",
    ]


def test_json_ld_description_escaped_twice_is_unescaped_once() -> None:
    escaped = DESCRIPTION.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    posting = {**POSTING, "description": escaped}

    lines = _lines(_page_text()(_page(_with_json_ld(json.dumps(posting)))))

    assert "Grow tidy datasets & prune spreadsheets." in lines
    assert "- Three years of SQL" in lines
    assert not any("<" in line for line in lines), lines


def test_js_shell_page_has_no_job_text() -> None:
    page_text = _page_text()

    assert page_text(_fixture_page("js_shell")).strip() == ""
    assert page_text(_page("")).strip() == ""
    shell = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
    assert page_text(_page(shell)).strip() == ""


# Structured HTML from a small tag vocabulary. Text never holds "<", ">" or "&", so any "<"
# in the result would be markup that leaked through.
TEXT = st.text(
    alphabet=st.characters(blacklist_characters="<>&", blacklist_categories=("Cs",)),
    max_size=30,
)
CONTAINERS = ["p", "div", "span", "h1", "h2", "h3", "ul", "ol", "li", "strong", "em", "a"]
DROPPED = ["script", "style", "nav", "noscript", "template"]
ATTRIBUTES = [
    "",
    ' class="x"',
    " hidden",
    ' aria-hidden="true"',
    ' style="display:none"',
    ' style="visibility: hidden"',
    ' href="/jobs?a=1"',
]
VOIDS = ["<br>", "<hr>", '<img src="x.png" alt="">', "<input>", "<meta charset=utf-8>"]


def _element(children: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    return st.builds(
        lambda tag, attributes, inner: f"<{tag}{attributes}>{inner}</{tag}>",
        st.sampled_from(CONTAINERS + DROPPED),
        st.sampled_from(ATTRIBUTES),
        st.lists(children, max_size=4).map("".join),
    )


HTML = st.recursive(
    st.one_of(TEXT, st.sampled_from(VOIDS)),
    _element,
    max_leaves=25,
).map(lambda body: f"<html><head><title>T</title></head><body>{body}</body></html>")


@settings(max_examples=300, deadline=None)
@given(
    document=st.one_of(
        HTML.map(lambda html: (html, "text/html", True)),
        st.text(max_size=300).map(lambda junk: (junk, "text/html", False)),
        st.text(max_size=300).map(lambda junk: ("<p>" + junk, "application/xhtml+xml", False)),
    )
)
def test_page_text_never_raises_and_has_no_tags(document: tuple[str, str, bool]) -> None:
    html, content_type, structured = document

    text = _page_text()(_page(html, content_type))

    assert isinstance(text, str)
    if structured:
        assert "<" not in text, (html, text)
        for line in text.split("\n"):
            assert "  " not in line, ("whitespace runs are collapsed", text)
