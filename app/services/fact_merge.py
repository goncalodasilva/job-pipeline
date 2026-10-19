"""The merge screen of a draft (M3.6; AC-M3.2): what it shows and which wording a posted
choice settles on. ``FactService.merge`` and ``undo_merge`` do the writing.

A draft is folded into a confirmed fact only; never into another draft, itself, an archived
or a discarded fact, and ``FactService.merge`` refuses the same. The screen is
a search with candidates, then a comparison of the two wordings with what made each, then the
result, with "Undo merge" for as long as the merge can be taken back. Everything here reads
only. Wording is the user's own or AI-made text: it is handed on as plain strings for the
templates to escape, and nothing of it is logged.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.domain.errors import FactInputInvalid, FactNotFound, FactTransitionRefused
from app.domain.facts import FactContent, FactStatus, clean_part
from app.domain.ids import is_safe_id
from app.services.fact_editor import Choice, Typed, one
from app.services.fact_library import LibraryQuery
from app.services.fact_views import (
    MSG_NO_MATCH,
    EditField,
    FactBank,
    FactItem,
    edit_fields,
    fact_item,
    found_message,
    open_bank,
    role_labels,
)
from app.services.facts import FactView

MSG_MERGED: Final = "Draft merged into the fact."
MSG_MERGE_UNDONE: Final = "Merge undone. The draft is back in Drafts to review."

MSG_NOT_A_DRAFT: Final = "This fact isn't a draft any more, so it can't be merged from here."

SEARCH: Final = "search"
COMPARE: Final = "compare"
RESULT: Final = "result"

KEEP_TARGET: Final = "target"
TAKE_DRAFT: Final = "draft"
EDITED: Final = "edited"
_CHOICES: Final[tuple[tuple[str, str, str], ...]] = (
    (KEEP_TARGET, "Keep the existing fact's wording", "The draft is closed; its skills are kept."),
    (TAKE_DRAFT, "Use the draft's wording", "The fact reads as the draft does now."),
    (EDITED, "Write the wording myself", "Use the field below."),
)
_LEGEND: Final = "Which wording should the fact keep?"
_CHOICE_FIELD: Final = "wording"
_CHOICE_NEEDED: Final = "Choose which wording to keep."
_TYPED_NOT_CHOSEN: Final = (
    'You changed the wording below. Choose "Write the wording myself" to use it, '
    "or undo your change."
)
# What can be merged into: a confirmed fact, never another draft.
_TARGET_STATUS: Final = FactStatus.CONFIRMED
_NOTE_BAD_TARGET: Final = "That fact can't be merged into. Choose another one."
_NOTE_TYPES_DIFFER: Final = (
    "One of these is a story and the other isn't, so the draft's wording can't be taken as it "
    "is. To use parts of it, write the wording yourself."
)


@dataclass(frozen=True)
class MergeView:
    state: str
    """``SEARCH``, ``COMPARE`` or ``RESULT`` (the draft was closed by a merge)."""
    draft: FactItem
    q: str = ""
    candidates: tuple[FactItem, ...] = ()
    more: bool = False
    """The list is cut short: a search narrows it."""
    note: str | None = None
    target: FactItem | None = None
    choices: tuple[Choice, ...] = ()
    fields: tuple[EditField, ...] = ()
    """The parts "Write the wording myself" takes: the target's own."""
    error_field: str | None = None
    error_message: str | None = None
    can_undo: bool = False
    legend: str = _LEGEND
    writing: bool = False
    """"Write the wording myself" is the checked choice: its fields are shown."""
    found: str = ""
    """What a search found, in words for the live region (``SEARCH`` only)."""


def _mergeable(draft: FactView, target: FactView) -> bool:
    return target.id != draft.id and target.status is _TARGET_STATUS


class FactMerge:
    """``bank`` answers the open workspace's fact services (None while none is open)."""

    def __init__(self, *, bank: Callable[[], FactBank | None]) -> None:
        self._bank = bank

    def page(
        self,
        draft_id: str,
        *,
        q: str = "",
        target_id: str = "",
        typed: Typed | None = None,
        error: FactInputInvalid | None = None,
    ) -> MergeView:
        """The screen for one draft. Raises ``FactNotFound``, and ``FactTransitionRefused``
        for a fact that is neither a draft nor one a merge closed."""
        bank = open_bank(self._bank)
        draft = bank.facts.get(draft_id)
        roles = role_labels(bank)
        item = fact_item(draft, roles)
        if draft.status is FactStatus.DISCARDED and draft.merged_into_fact_id is not None:
            target = bank.facts.get(draft.merged_into_fact_id)
            can_undo = bank.facts.can_undo_merge(draft_id)
            return MergeView(RESULT, item, target=fact_item(target, roles), can_undo=can_undo)
        if draft.status is not FactStatus.DRAFT:
            raise FactTransitionRefused(detail=f"fact {draft_id} is {draft.status}: no merge")
        chosen = self._target(bank, draft, target_id)
        if chosen is None:
            note = _NOTE_BAD_TARGET if target_id else None
            found, more = self._candidates(bank, draft, q)
            candidates = tuple(fact_item(view, roles) for view in found)
            said = found_message(len(candidates)) if candidates else MSG_NO_MATCH
            return MergeView(
                SEARCH, item, q=q, candidates=candidates, more=more, note=note, found=said
            )
        return _comparison(draft, chosen, item, fact_item(chosen, roles), typed, error)

    def _target(self, bank: FactBank, draft: FactView, target_id: str) -> FactView | None:
        """The fact ``target_id`` names, when the draft can be merged into it. A value that
        isn't of an id's shape (``is_safe_id``) names nothing and is never looked up."""
        if not is_safe_id(target_id):
            return None
        try:
            target = bank.facts.get(target_id)
        except FactNotFound:
            return None
        return target if _mergeable(draft, target) else None

    def _candidates(self, bank: FactBank, draft: FactView, q: str) -> tuple[list[FactView], bool]:
        """Confirmed facts that match ``q`` (the first page), and whether there are more."""
        page = bank.library.search(LibraryQuery(text=q, status=_TARGET_STATUS))
        found = [view for view in page.items if view.id != draft.id]
        return found, page.total > len(page.items)

    def settle(
        self, draft_id: str, target_id: str, typed: Typed, *, target_version: int
    ) -> FactContent:
        """The wording a posted comparison settles on: the target's, the draft's (same type
        only) or the typed one, read from the fields the form shows. Raises ``FactNotFound``;
        ``FactTransitionRefused`` for a target the screen never offers (another draft, the
        draft itself, an archived or a discarded fact); and ``FactInputInvalid``: for a choice
        that isn't one, and for wording that was typed while another choice is made, which
        would otherwise be dropped without a word.
        ``target_version`` is the one the form carried: against any other the fields prove
        nothing (the page filled them in from older wording), and ``FactService.merge``,
        which checks the rest, refuses that form as stale."""
        bank = open_bank(self._bank)
        draft = bank.facts.get(draft_id)
        target = bank.facts.get(target_id)
        if not _mergeable(draft, target):
            raise FactTransitionRefused(
                detail=f"merge into a {target.status} fact is not offered: confirmed only"
            )
        choice = one(typed, _CHOICE_FIELD)
        if choice == EDITED:
            shown = {field.name: one(typed, field.name) for field in edit_fields(target)}
            return FactContent(
                shown.get("text"), shown.get("situation"), shown.get("action"), shown.get("result")
            )
        taken = draft.type is target.type and choice == TAKE_DRAFT
        if choice != KEEP_TARGET and not taken:
            raise FactInputInvalid(_CHOICE_FIELD, _CHOICE_NEEDED)
        if target.version == target_version and _typed_differs(target, typed):
            raise FactInputInvalid(_CHOICE_FIELD, _TYPED_NOT_CHOSEN)
        return draft.content if taken else target.content


def _typed_differs(target: FactView, typed: Typed) -> bool:
    """Whether a wording field the form sent no longer reads as the page filled it in (the
    target's wording). A field that wasn't sent changed nothing."""
    return any(
        field.name in typed and clean_part(one(typed, field.name)) != clean_part(field.value)
        for field in edit_fields(target)
    )


def _comparison(
    draft: FactView,
    target: FactView,
    draft_item: FactItem,
    target_item: FactItem,
    typed: Typed | None,
    error: FactInputInvalid | None,
) -> MergeView:
    """Both wordings and the form: the choices (the draft's wording only between two of the
    same type), the first chosen unless a refused form chose another, and the fields for a
    wording of the user's own, which start from the target's."""
    same_type = draft.type is target.type
    offered = [choice for choice in _CHOICES if same_type or choice[0] != TAKE_DRAFT]
    wanted = one(typed, _CHOICE_FIELD) if typed is not None else KEEP_TARGET
    chosen = wanted if any(wanted == value for value, _label, _help in offered) else KEEP_TARGET
    fields = edit_fields(target)
    if typed is not None:
        fields = tuple(
            EditField(field.name, field.label, one(typed, field.name)) for field in fields
        )
    names = [field.name for field in fields]
    error_field = None
    if error is not None:
        known = error.field in names or error.field == _CHOICE_FIELD
        error_field = error.field if known else names[0]
    return MergeView(
        COMPARE,
        draft_item,
        note=None if same_type else _NOTE_TYPES_DIFFER,
        target=target_item,
        choices=tuple(
            Choice(value, label, explained, value == chosen) for value, label, explained in offered
        ),
        fields=fields,
        writing=chosen == EDITED,
        error_field=error_field,
        error_message=None if error is None else error.user_message,
    )
