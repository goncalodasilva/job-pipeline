"""The command palette (M9.5 part A; ADR-098 item 7; spec 2.9 rules 2, 3 and 12).

Two reads, no writes: what to show for typed text, and the titles of the recent item pages the
browser remembers. Typed text, paths and the rows found are untrusted data: nothing here logs
them, stores them or turns them into markup (the template escapes). A page path is resolved
only if it is the exact page of one item with a safe id that exists.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.domain.fts import fts_prefix_query
from app.domain.ids import is_safe_id
from app.domain.palette import (
    ACTIONS,
    KINDS,
    MAX_RESULTS,
    PaletteAction,
    PaletteItem,
    allocate,
    clean_query,
    company_matches,
    matching_actions,
    recent_targets,
    words,
)
from app.modules.storage import Database
from app.modules.storage import palette_repo as repo


@dataclass(frozen=True)
class PaletteGroup:
    """One kind of answer: ``job``, ``company``, ``application``, ``fact``, ``recent`` or
    ``actions``. The items are ``PaletteAction`` only in the actions group."""

    kind: str
    items: tuple[PaletteItem, ...] = ()
    actions: tuple[PaletteAction, ...] = ()


@dataclass(frozen=True)
class PaletteAnswer:
    groups: tuple[PaletteGroup, ...]
    searched: bool = False  # text was typed: the answer counts results and may be empty
    results: int = 0
    actions: int = 0

    @property
    def empty(self) -> bool:
        return self.searched and self.results == 0 and self.actions == 0


class PaletteService:
    def __init__(self, database: Callable[[], Database | None]) -> None:
        self._database = database

    def search(self, text: str) -> PaletteAnswer:
        """Jobs, companies, applications and facts for ``text`` (at most ``MAX_RESULTS`` in all,
        one group per kind) and the actions it names. Blank text lists the actions only."""
        query = clean_query(text)
        actions = matching_actions(query)
        match = fts_prefix_query(query)
        groups: list[PaletteGroup] = []
        count = 0
        if match is not None:
            found = self._found(query, match)
            for kind, items in allocate(found, MAX_RESULTS).items():
                if items:
                    groups.append(PaletteGroup(kind, tuple(items)))
                    count += len(items)
        if actions:
            groups.append(PaletteGroup("actions", actions=actions))
        return PaletteAnswer(
            tuple(groups), searched=match is not None, results=count, actions=len(actions)
        )

    def recent(self, paths: Sequence[str]) -> PaletteAnswer:
        """The titles of the recent item pages among ``paths`` (at most ``MAX_RECENT``, in the
        order sent) and all the actions. A path that isn't an existing item page is dropped."""
        items: list[PaletteItem] = []
        db = self._database()
        targets = recent_targets(paths)
        if db is not None and targets:
            with db.read() as conn:
                for kind, item_id in targets:
                    item = repo.recent_item(conn, kind, item_id)
                    if item is not None:
                        items.append(item)
        groups = [PaletteGroup("recent", tuple(items))] if items else []
        groups.append(PaletteGroup("actions", actions=ACTIONS))
        return PaletteAnswer(tuple(groups), actions=len(ACTIONS))

    def _found(self, query: str, match: str) -> dict[str, list[PaletteItem]]:
        found: dict[str, list[PaletteItem]] = {kind: [] for kind in KINDS}
        db = self._database()
        if db is None:
            return found
        typed = words(query)
        with db.read() as conn:
            found["job"] = repo.jobs(conn, match, MAX_RESULTS)
            found["company"] = [
                item for item in repo.companies(conn) if company_matches(item.title, typed)
            ][:MAX_RESULTS]
            found["application"] = repo.applications(conn, match, MAX_RESULTS)
            found["fact"] = repo.facts(conn, match, MAX_RESULTS)
        return {
            kind: [item for item in items if is_safe_id(item.id)] for kind, items in found.items()
        }
