"""The command palette's rules (M9.5 part A; ADR-098 item 7; spec 2.9 rules 2, 3 and 12). Pure.

What the palette shows, in which order and how much of it; which pages count as recent items;
which actions it offers. Typed text and page paths are untrusted: nothing here echoes them.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, TypeVar

from app.domain.ids import is_safe_id

KINDS: Final = ("job", "company", "application", "fact")
MAX_RESULTS: Final = 20
MAX_RECENT: Final = 8
MAX_RECENT_SCANNED: Final = 64  # raw paths looked at, so a long list costs a bounded amount
MAX_QUERY_CHARS: Final = 200  # a longer query is cut, never refused (an htmx swap of a 4xx fails)
MAX_WORDS: Final = 8
TITLE_CHARS: Final = 100

_ID: Final = "[A-Za-z0-9_-]{1,64}"
_ITEM_PAGE: Final = re.compile(rf"/(jobs|companies|applications)/({_ID})|/facts/({_ID})/edit")
_KIND_OF_SEGMENT: Final = {"jobs": "job", "companies": "company", "applications": "application"}
_SEGMENT_OF_KIND: Final = {kind: segment for segment, kind in _KIND_OF_SEGMENT.items()}
_WORD: Final = re.compile(r"\w+")

T = TypeVar("T")


@dataclass(frozen=True)
class PaletteItem:
    """One job, company, application or fact: its title, a second line when there is one,
    and its state when it is not the usual one."""

    kind: str
    id: str
    title: str
    meta: str = ""
    state: str = ""  # "removed" (a job), "draft" or "archived" (a fact); said in words

    @property
    def href(self) -> str:
        return item_path(self.kind, self.id)


@dataclass(frozen=True)
class PaletteAction:
    key: str
    label: str
    href: str


ACTIONS: Final = (
    PaletteAction("add-job", "Add job", "/inbox#add-job"),
    PaletteAction("new-fact", "New fact", "/facts/new"),
    PaletteAction("start-session", "Start session", "/sessions/new"),
    PaletteAction("new-event", "New event", "/board"),
)


def item_path(kind: str, item_id: str) -> str:
    """The page of an item. Only call it with a safe id."""
    if kind == "fact":
        return f"/facts/{item_id}/edit"
    return f"/{_SEGMENT_OF_KIND[kind]}/{item_id}"


def recent_target(path: str) -> tuple[str, str] | None:
    """(kind, id) when ``path`` is exactly the page of one item, else ``None``: a foreign or
    protocol-relative address, a query or fragment, a traversal, another page and an unsafe id
    all name nothing."""
    found = _ITEM_PAGE.fullmatch(path)
    if found is None:
        return None
    if found.group(3) is not None:
        kind, item_id = "fact", found.group(3)
    else:
        kind, item_id = _KIND_OF_SEGMENT[found.group(1)], found.group(2)
    return (kind, item_id) if is_safe_id(item_id) else None


def recent_targets(paths: Sequence[str]) -> list[tuple[str, str]]:
    """The first ``MAX_RECENT`` distinct item pages among the first ``MAX_RECENT_SCANNED`` paths,
    in the order sent."""
    kept: list[tuple[str, str]] = []
    for path in paths[:MAX_RECENT_SCANNED]:
        target = recent_target(path)
        if target is not None and target not in kept:
            kept.append(target)
    return kept[:MAX_RECENT]


def clean_query(text: str) -> str:
    """At most ``MAX_QUERY_CHARS`` characters and ``MAX_WORDS`` words of ``text``, so the full-text
    search asks for a bounded number of words (ADR-138 item 8)."""
    return " ".join(text[:MAX_QUERY_CHARS].split()[:MAX_WORDS])


def words(text: str) -> list[str]:
    """The words of ``text``, lower-cased, at most ``MAX_WORDS``."""
    return _WORD.findall(text.casefold())[:MAX_WORDS]


def _all_words_start(typed: Sequence[str], text: str) -> bool:
    known = _WORD.findall(text.casefold())
    return all(any(word.startswith(piece) for word in known) for piece in typed)


def matching_actions(query: str) -> tuple[PaletteAction, ...]:
    """Every action when nothing was typed; else those whose label has a word starting with
    each typed word. Typed text with no word in it (only symbols) matches none."""
    if not query.strip():
        return ACTIONS
    typed = words(query)
    if not typed:
        return ()
    return tuple(action for action in ACTIONS if _all_words_start(typed, action.label))


def company_matches(name: str, typed: Sequence[str]) -> bool:
    """Whether each typed word starts a word of the company's name, ignoring case."""
    return bool(typed) and _all_words_start(typed, name)


def shortened(text: str) -> str:
    """One line of at most ``TITLE_CHARS`` characters (a fact's text is its title)."""
    line = " ".join(text.split())
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 1].rstrip() + "…"


def allocate(found: Mapping[str, Sequence[T]], limit: int = MAX_RESULTS) -> dict[str, list[T]]:
    """At most ``limit`` results in all, taken one kind at a time in turn so a long list of jobs
    doesn't push out the one matching fact. Each kind keeps its own order."""
    taken: dict[str, list[T]] = {kind: [] for kind in found}
    total = 0
    depth = 0
    while total < limit and any(depth < len(rows) for rows in found.values()):
        for kind, rows in found.items():
            if depth < len(rows) and total < limit:
                taken[kind].append(rows[depth])
                total += 1
        depth += 1
    return taken
