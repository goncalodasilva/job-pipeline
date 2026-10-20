"""The readable text of a fetched job page (spec 2.4, 3.3; ADR-098 item 2).

Stdlib ``html.parser`` and ``json`` only. ``page_text`` keeps headings, paragraphs, table
cells and list items as separate lines (list items start with "- "), collapses whitespace
and unescapes entities. Script, style, nav, noscript, template and the document head are
dropped, as are hidden parts: the ``hidden`` attribute, ``aria-hidden="true"`` and inline
``display:none`` or ``visibility:hidden``. When the page carries a JSON-LD ``JobPosting``
with a description, its title and description are used instead of the page body, which is
usually board chrome. A ``text/plain`` page comes back with its lines kept and their
whitespace collapsed. A JavaScript-only shell has no text.

The page is untrusted content (spec 3.3): nothing in it is followed or executed, and this
never raises on malformed markup or JSON. Over-complex markup is refused whole.
"""

import html
import json
import math
from collections.abc import Iterator, Sequence
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from app.modules.fetch import FetchedPage

__all__ = ["page_text"]

PLAIN_TEXT: Final = "text/plain"
_DROPPED: Final = frozenset(
    {"head", "iframe", "nav", "noscript", "object", "script", "style", "svg", "template"}
)
_BLOCKS: Final = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "br",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "html",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
# Elements without an end tag: never kept open, so they can't hide what follows.
_VOID: Final = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
# Implied end tags (a simplified HTML tree builder): a start tag closes the nearest open
# element it names, unless a boundary element is opened inside that one first.
_IMPLIED_ENDS: Final[dict[str, tuple[frozenset[str], frozenset[str]]]] = {
    "li": (frozenset({"li"}), frozenset({"ul", "ol", "menu", "table"})),
    "dt": (frozenset({"dt", "dd"}), frozenset({"dl", "table"})),
    "dd": (frozenset({"dt", "dd"}), frozenset({"dl", "table"})),
    "tr": (frozenset({"tr", "td", "th"}), frozenset({"table"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "option": (frozenset({"option"}), frozenset({"select", "datalist"})),
}
# Block starts that close an open paragraph, and what keeps a paragraph out of reach.
_CLOSES_PARAGRAPH: Final = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "menu",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "ul",
    }
)
_PARAGRAPH_SCOPE: Final = frozenset(
    {"button", "caption", "html", "object", "table", "td", "template", "th"}
)
_PARAGRAPH: Final = frozenset({"p"})
_JSON_LD: Final = "application/ld+json"
_LIST_ITEM_PREFIX: Final = "- "
_MAX_OPEN_ELEMENTS: Final = 256
_MAX_STACK_STEPS: Final = 1_000_000


class _MarkupTooComplex(Exception):
    """The parser refuses deep trees and excessive stack scans, without partial text."""


def page_text(page: "FetchedPage") -> str:
    """The page's text, one block per line; empty when it has none."""
    if page.content_type == PLAIN_TEXT:
        return "\n".join(_plain_lines(page.text))
    try:
        parsed = _parse(page.text)
        posting = _job_posting_lines(parsed.json_ld)
        return "\n".join(posting if posting is not None else parsed.lines)
    except _MarkupTooComplex:
        return ""


def _plain_lines(text: str) -> list[str]:
    return [line for line in (_collapse(raw) for raw in text.splitlines()) if line]


def _collapse(text: str) -> str:
    return " ".join(text.split())


class _TextParser(HTMLParser):
    """Collects visible text lines and the raw JSON-LD blocks of an HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.json_ld: list[str] = []
        self._current: list[str] = []
        self._prefix = ""
        self._open: list[tuple[str, bool]] = []  # (tag, dropped) for every open element
        self._dropped_depth = 0
        self._stack_steps = 0
        self._json_ld_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._close_implied(tag)
        if tag in _VOID:
            self._block_boundary(tag)
            return
        if tag == "body":
            self._close("head")  # "</head>" may be left out
        if tag == "script" and _attribute(attrs, "type").strip().lower() == _JSON_LD:
            self._json_ld_parts = []
        dropped = tag in _DROPPED or _is_hidden(attrs)
        if len(self._open) >= _MAX_OPEN_ELEMENTS:
            raise _MarkupTooComplex()
        self._open.append((tag, dropped))
        if dropped:
            self._dropped_depth += 1
        self._block_boundary(tag)
        if tag == "li" and not self._dropped_depth:
            self._prefix = _LIST_ITEM_PREFIX

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # "<div/>" in HTML is a start tag; only void elements end themselves.
        if tag in _VOID:
            self._block_boundary(tag)
        else:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if tag == "script" and self._json_ld_parts is not None:
            self.json_ld.append("".join(self._json_ld_parts))
            self._json_ld_parts = None
        self._close(tag)
        if tag in _BLOCKS and not self._dropped_depth:
            self.flush()
            self._prefix = ""

    def handle_data(self, data: str) -> None:
        if self._json_ld_parts is not None:
            self._json_ld_parts.append(data)
        elif not self._dropped_depth:
            self._current.append(data)

    def flush(self) -> None:
        """End the current line, if it has any text."""
        line = _collapse("".join(self._current))
        self._current = []
        if line:
            self.lines.append(self._prefix + line)
            self._prefix = ""

    def _block_boundary(self, tag: str) -> None:
        if tag in _BLOCKS and not self._dropped_depth:
            self.flush()

    def _close(self, tag: str) -> None:
        """Close ``tag`` and everything opened inside it; a stray end tag is ignored."""
        self._close_within(frozenset({tag}), frozenset())

    def _close_implied(self, tag: str) -> None:
        """Close what ``tag`` ends implicitly ("<li>" an open "li", "<div>" an open "p")."""
        if tag in _CLOSES_PARAGRAPH:
            self._close_within(_PARAGRAPH, _PARAGRAPH_SCOPE)
        rule = _IMPLIED_ENDS.get(tag)
        if rule is not None:
            self._close_within(*rule)

    def _close_within(self, names: frozenset[str], boundary: frozenset[str]) -> None:
        """Close the innermost open element in ``names``, with everything inside it, unless
        an element in ``boundary`` comes first."""
        for index in range(len(self._open) - 1, -1, -1):
            self._stack_steps += 1
            if self._stack_steps > _MAX_STACK_STEPS:
                raise _MarkupTooComplex()
            name = self._open[index][0]
            if name in names:
                self._dropped_depth -= sum(dropped for _, dropped in self._open[index:])
                del self._open[index:]
                return
            if name in boundary:
                return


def _attribute(attrs: Sequence[tuple[str, str | None]], name: str) -> str:
    for key, value in attrs:
        if key == name:
            return value or ""
    return ""


def _is_hidden(attrs: Sequence[tuple[str, str | None]]) -> bool:
    names = {key for key, _ in attrs}
    if "hidden" in names:
        return True
    if _attribute(attrs, "aria-hidden").strip().lower() == "true":
        return True
    style = "".join(_attribute(attrs, "style").lower().split())
    return "display:none" in style or "visibility:hidden" in style


def _parse(markup: str) -> _TextParser:
    parser = _TextParser()
    parser.feed(markup)
    parser.close()
    parser.flush()
    return parser


def _job_posting_lines(blocks: Sequence[str]) -> list[str] | None:
    """The title and description lines of the first JSON-LD ``JobPosting`` with a
    description, or ``None``."""
    for block in blocks:
        try:
            posting = _first_described_posting(json.loads(block))
        except (ValueError, RecursionError):  # broken or absurdly nested JSON
            continue
        if posting is not None:
            title = _text(posting.get("title"))
            lines = [title] if title is not None else []
            return lines + _posting_details(posting) + _description_lines(posting["description"])
    return None


def _posting_details(posting: dict[str, Any]) -> list[str]:
    """Company, locations, employment type, salary and posting date, each on its own line
    when present in the expected shape; other shapes are skipped."""
    details = [
        _labelled("Company", _name(posting.get("hiringOrganization"))),
        *(_labelled("Location", place) for place in _locations(posting.get("jobLocation"))),
        _labelled("Employment type", _employment_type(posting.get("employmentType"))),
        _labelled("Salary", _salary(posting.get("baseSalary"))),
        _labelled("Posted", _text(posting.get("datePosted"))),
    ]
    return [line for line in details if line is not None]


def _labelled(label: str, value: str | None) -> str | None:
    return f"{label}: {value}" if value is not None else None


def _text(value: Any) -> str | None:
    """A non-blank string, whitespace collapsed; ``None`` for anything else."""
    return _collapse(value) if isinstance(value, str) and value.strip() else None


def _name(value: Any) -> str | None:
    """A string, or the ``name`` of an object (an Organization, a Country)."""
    return _text(value.get("name")) if isinstance(value, dict) else _text(value)


def _locations(value: Any) -> list[str]:
    places = value if isinstance(value, list) else [value]
    found = (_place(place) for place in places if isinstance(place, dict))
    return [place for place in found if place is not None]


def _place(place: dict[str, Any]) -> str | None:
    address = place.get("address")
    if not isinstance(address, dict):
        return _text(address)
    parts = [
        _text(address.get("addressLocality")),
        _text(address.get("addressRegion")),
        _name(address.get("addressCountry")),
    ]
    joined = ", ".join(part for part in parts if part is not None)
    return joined or None


def _employment_type(value: Any) -> str | None:
    kinds = value if isinstance(value, list) else [value]
    found = [kind for kind in (_text(item) for item in kinds) if kind is not None]
    return ", ".join(found) or None


def _salary(value: Any) -> str | None:
    """ "50000 to 60000 EUR per YEAR" from a MonetaryAmount; ``None`` without an amount."""
    if not isinstance(value, dict):
        return None
    amount = value.get("value")
    unit = None
    if isinstance(amount, dict):
        unit = _text(amount.get("unitText"))
        single = _number(amount.get("value"))
        low, high = _number(amount.get("minValue")), _number(amount.get("maxValue"))
    else:
        single, low, high = _number(amount), None, None
    if single is not None:
        figure = single
    elif low is not None and high is not None:
        figure = f"{low} to {high}"
    elif low is not None:
        figure = f"from {low}"
    elif high is not None:
        figure = f"up to {high}"
    else:
        return None
    currency = _text(value.get("currency"))
    return " ".join(
        part
        for part in (figure, currency, f"per {unit}" if unit is not None else None)
        if part is not None
    )


def _number(value: Any) -> str | None:
    """A JSON number as written for people (no ".0" on whole numbers)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):  # NaN and Infinity parse
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _first_described_posting(data: Any) -> dict[str, Any] | None:
    for posting in _postings(data):
        description = posting.get("description")
        if isinstance(description, str) and description.strip():
            return posting
    return None


def _postings(node: Any) -> Iterator[dict[str, Any]]:
    """Every ``JobPosting`` object in a JSON-LD value: the value itself, a list of values or
    an ``@graph``."""
    if isinstance(node, list):
        for item in node:
            yield from _postings(item)
    elif isinstance(node, dict):
        kind = node.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            yield node
        yield from _postings(node.get("@graph"))


def _description_lines(description: str) -> list[str]:
    # Some boards escape the description's HTML once more ("&lt;p&gt;").
    if "<" not in description and "&lt;" in description:
        description = html.unescape(description)
    return _parse(description).lines
