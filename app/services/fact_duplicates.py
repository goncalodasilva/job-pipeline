"""Possible duplicates on the Drafts tab (M3.7; ADR-089 item 7, ADR-094 and its 2026-10-16
update).

On demand, every draft is compared with every confirmed fact by the mechanical similarity of
``app.domain.similarity``: no AI call and no task. A pair is listed when the two wordings are
alike, unless the user dismissed it ("Not a duplicate"), which is remembered in
``fact_pair_dismissals`` until the user takes it back (Undo). Only a draft and a confirmed fact
make a pair, the merge screen's rule: a draft merges only into a confirmed fact. Wordings are
handed on as plain strings for the templates to escape, and nothing of them is logged.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.domain.clock import Clock
from app.domain.errors import FactNotFound, FactTransitionRefused
from app.domain.facts import FactStatus
from app.domain.ids import is_safe_id
from app.domain.similarity import SUGGEST_AT, words, words_similarity
from app.modules.storage import fact_pairs_repo, facts_repo
from app.services.fact_views import FactBank, FactItem, fact_item, open_bank, role_labels
from app.services.facts import fact_view

MSG_DISMISSED: Final = "Suggestion dismissed."
MSG_UNDISMISSED: Final = "Suggestion restored."
MSG_NONE: Final = "No possible duplicates found."
# A dismissal of a pair that is no longer a draft and a confirmed fact (changed elsewhere).
MSG_PAIR_CHANGED: Final = "That pair has changed. The list is updated."
# The region after the drafts changed on the page: its pairs may be out of date.
MSG_STALE: Final = (
    "Your drafts changed. Find possible duplicates again to see the list as it is now."
)


@dataclass(frozen=True)
class DuplicatePair:
    draft: FactItem
    fact: FactItem
    draft_wording: str
    fact_wording: str


@dataclass(frozen=True)
class DuplicatesView:
    """What the region shows: the pairs, how many in words and, after an action, what it
    did (``status``) and the pair its Undo brings back (``undo``)."""

    pairs: tuple[DuplicatePair, ...]
    status: str | None = None
    undo: tuple[str, str] | None = None
    stale: bool = False
    """The drafts changed since the list was made: no pairs, only a line saying so."""

    @property
    def found(self) -> str:
        count = len(self.pairs)
        pairs = "1 possible duplicate" if count == 1 else f"{count} possible duplicates"
        return f"{pairs} found. Compare each pair, or say it isn't one."

    @property
    def said(self) -> str:
        """The words for the live region."""
        if self.status:
            return self.status
        return self.found if self.pairs else MSG_NONE


def _wording(item: FactItem) -> str:
    """Everything the fact says, as one text: a fact's wording or a story's parts."""
    return " ".join(field.value for field in item.fields if field.value)


def _ids(draft_id: str, fact_id: str) -> None:
    if not (is_safe_id(draft_id) and is_safe_id(fact_id)):
        raise FactNotFound(detail="not a fact id")


class FactDuplicates:
    """``bank`` answers the open workspace's fact services (None while none is open)."""

    def __init__(self, *, bank: Callable[[], FactBank | None], clock: Clock) -> None:
        self._bank = bank
        self._clock = clock

    def view(
        self, *, status: str | None = None, undo: tuple[str, str] | None = None
    ) -> DuplicatesView:
        """The region's content now; ``status`` says what was just done. Reads only."""
        return DuplicatesView(self.pairs(), status, undo)

    @staticmethod
    def stale() -> DuplicatesView:
        """The region once the drafts changed on the page. Reads nothing."""
        return DuplicatesView((), MSG_STALE, stale=True)

    def pairs(self) -> tuple[DuplicatePair, ...]:
        """Each draft and confirmed fact whose wordings are alike, in the order the drafts
        and then the facts were made, without the dismissed pairs. Reads only."""
        bank = open_bank(self._bank)
        roles = role_labels(bank)
        with bank.db.read() as conn:
            drafts = [
                fact_item(fact_view(conn, fact_id), roles)
                for fact_id in facts_repo.ids_with_status(conn, FactStatus.DRAFT.value)
            ]
            facts = [
                fact_item(fact_view(conn, fact_id), roles)
                for fact_id in facts_repo.ids_with_status(conn, FactStatus.CONFIRMED.value)
            ]
            dismissed = fact_pairs_repo.dismissed(conn)
        wordings = {item.id: _wording(item) for item in (*drafts, *facts)}
        bags = {fact_id: words(text) for fact_id, text in wordings.items()}  # once per fact
        return tuple(
            DuplicatePair(draft, fact, wordings[draft.id], wordings[fact.id])
            for draft in drafts
            for fact in facts
            if fact_pairs_repo.sorted_pair(draft.id, fact.id) not in dismissed
            and words_similarity(bags[draft.id], bags[fact.id]) >= SUGGEST_AT
        )

    def dismiss(self, draft_id: str, fact_id: str) -> None:
        """Remember that a draft and a confirmed fact are not duplicates. Raises
        ``FactNotFound`` for an id that names no fact and ``FactTransitionRefused`` when the
        first isn't a draft or the second isn't confirmed; nothing is stored then."""
        _ids(draft_id, fact_id)
        bank = open_bank(self._bank)
        with bank.db.transaction() as conn:
            draft = fact_view(conn, draft_id)
            fact = fact_view(conn, fact_id)
            if draft.status is not FactStatus.DRAFT or fact.status is not FactStatus.CONFIRMED:
                raise FactTransitionRefused(detail="not a draft and a confirmed fact")
            fact_pairs_repo.dismiss(conn, draft_id, fact_id, now=self._clock.now().isoformat())

    def undismiss(self, draft_id: str, fact_id: str) -> None:
        """Forget a dismissal (Undo), so the pair is listed again while it is alike. Raises
        ``FactNotFound`` for an id that names no fact; a pair never dismissed is left as it
        is."""
        _ids(draft_id, fact_id)
        bank = open_bank(self._bank)
        with bank.db.transaction() as conn:
            fact_view(conn, draft_id)
            fact_view(conn, fact_id)
            fact_pairs_repo.undismiss(conn, draft_id, fact_id)
