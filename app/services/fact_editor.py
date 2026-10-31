"""The fact editor (M3.6): what its form shows and what a posted one means.

``FactEditor`` turns a fact (or nothing yet) into the form's fields, and a posted form into a
``FactInput`` that ``FactService`` saves in one transaction, so routes and templates hold no
rules. A refused form is shown back with what was typed. The type is fixed once a fact exists,
and only the wording fields the form shows are read: any other is ignored. Names and lists
longer than a fact takes (``MAX_NAME_CHARS`` and the others) are refused on their own field.
A draft is shown with what made it, and the line a save leaves never calls it confirmed.

Wording, roles, skills and tags are the user's own or AI-made text: they are handed on as plain
strings for the templates to escape, and nothing of them is logged.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from app.domain.errors import FactInputInvalid
from app.domain.facts import FactStatus, FactType, Privacy, RoleStatus
from app.domain.ids import is_safe_id
from app.domain.quality_hints import quality_hints
from app.modules.storage import facts_repo
from app.services.capture import NoteView
from app.services.fact_views import (
    DRAFTS,
    LIBRARY,
    EditField,
    FactBank,
    FactItem,
    Option,
    edit_fields,
    fact_item,
    open_bank,
    role_label,
    role_labels,
)
from app.services.facts import FactInput, FactView, NewRole

Typed = Mapping[str, Sequence[str]]
"""A posted form: every value of each field, as it arrived."""

NOTE_FIELD: Final = "note"
"""The new-item form's hidden field naming the capture note it turns into a fact (M3.7)."""

MSG_ADDED: Final = "Fact added to your Library."
MSG_STORY_ADDED: Final = "Story added to your Library."
MSG_SAVED: Final = "Fact saved."
MSG_STORY_SAVED: Final = "Story saved."
# An edit is never the user's confirmation (ADR-091): the line must not read as one.
MSG_DRAFT_SAVED: Final = "Draft saved. It still needs your confirmation in Drafts to review."
MSG_ARCHIVED: Final = "Fact archived."
MSG_UNARCHIVED: Final = "Fact restored."

_NEW_HEADINGS: Final = {FactType.FACT: "Add a fact", FactType.STORY: "Add a story"}
_EDIT_HEADINGS: Final = {FactType.FACT: "Edit fact", FactType.STORY: "Edit story"}
# A draft that was closed has nothing to edit: its page says what it is.
_HEADING_DISCARDED: Final = "Discarded draft"
_HEADING_MERGED: Final = "Merged draft"
_WORDING: Final = {
    FactType.FACT: (("text", "Wording"),),
    FactType.STORY: (("situation", "Situation"), ("action", "Action"), ("result", "Result")),
}
_STORY_INTRO: Final = "Fill in what you have; you can add the rest later."
_HINTS: Final[Mapping[str, str]] = {
    "situation": "What was going on, in a sentence or two.",
    "action": "What you did yourself.",
    "result": "What changed, with a number if you have one.",
}
_BACK_LIBRARY: Final = (f"/facts?tab={LIBRARY}", "Back to the Library")
_BACK_DRAFTS: Final = (f"/facts?tab={DRAFTS}", "Back to Drafts to review")

# The longest names and lists a fact takes: those of the ``extract_cv`` schema.
MAX_NAME_CHARS: Final = 80
MAX_ROLE_CHARS: Final = 200
MAX_TAGS: Final = 30
MAX_SKILLS: Final = 60
_TAG_TOO_LONG: Final = f"Keep each tag to {MAX_NAME_CHARS} characters or fewer."
_TOO_MANY_TAGS: Final = f"A fact can have up to {MAX_TAGS} tags. Remove some."
_SKILL_TOO_LONG: Final = f"Keep each skill name to {MAX_NAME_CHARS} characters or fewer."
_TOO_MANY_SKILLS: Final = f"A fact can have up to {MAX_SKILLS} skills. Remove some."
_EMPLOYER_TOO_LONG: Final = f"Keep the employer to {MAX_ROLE_CHARS} characters or fewer."
_TITLE_TOO_LONG: Final = f"Keep the job title to {MAX_ROLE_CHARS} characters or fewer."
# What a refused save's other typed fields are called when they are shown back to be copied.
_KEPT_LABELS: Final = (
    ("new_role_employer", "Employer"),
    ("new_role_title", "Job title"),
    ("new_skills", "Add skills"),
    ("tags", "Tags"),
    ("start_date", "Start date"),
    ("end_date", "End date"),
)
_PRIVACY: Final = (
    (
        Privacy.NORMAL,
        "Normal",
        "The app may use this in your CVs and letters and send it to your AI provider.",
    ),
    (
        Privacy.NEVER_USE,
        "Never use in applications",
        "Kept for your own reference. It is never put in a CV or a letter "
        "and never sent to the AI.",
    ),
    (
        Privacy.GENERALIZE,
        "Generalize names",
        "The AI is told to describe names in general terms, such as "
        '"a large retail bank" instead of a client\'s name.',
    ),
)
PRIVACY_LABELS: Final[Mapping[str, str]] = {
    privacy.value: label for privacy, label, _help in _PRIVACY
}
# The fields an error can be tied to besides the wording.
_OTHER_FIELDS: Final = (
    "role_id",
    "new_role_employer",
    "new_role_title",
    "new_skills",
    "tags",
    "start_date",
    "end_date",
    "privacy",
)
_NO_VERSION: Final = 0  # versions start at 1, so this one is always stale


@dataclass(frozen=True)
class Choice:
    """One radio of a group, with its one-line explanation."""

    value: str
    label: str
    help: str
    checked: bool


@dataclass(frozen=True)
class SkillTick:
    name: str
    checked: bool


@dataclass(frozen=True)
class EditorView:
    fact_id: str | None
    """None for an item that doesn't exist yet."""
    kind: str
    heading: str
    action: str
    version: int | None
    status: str | None
    fields: tuple[EditField, ...]
    """The wording, one field per part. Empty when the fact can't be edited (discarded)."""
    roles: tuple[Option, ...]
    role_id: str
    new_role_employer: str
    new_role_title: str
    skills: tuple[SkillTick, ...]
    new_skills: str
    tags: str
    start_date: str
    end_date: str
    privacy: tuple[Choice, ...]
    error_field: str | None = None
    error_message: str | None = None
    item: FactItem | None = None
    """The fact as a card, shown instead of the form when it can't be edited."""
    merged: bool = False
    intro: str | None = None
    """A line under the heading (a story's parts need not all be filled in)."""
    draft: FactItem | None = None
    """A draft's own card data: what made it, its needs-check flag and the CV line."""
    back_url: str = _BACK_LIBRARY[0]
    back_label: str = _BACK_LIBRARY[1]
    kept: tuple[EditField, ...] = ()
    """What a refused save typed for a fact that has no form any more, to be copied."""
    note_id: str | None = None
    """The capture note a new fact is made from (M3.7): sent back in a hidden field."""
    hints: tuple[str, ...] = ()
    """Non-blocking quality hints of the fact as it is saved (M7.9), shown outside the form."""


def one(typed: Typed, name: str) -> str:
    """A field's single value as typed ("" when it wasn't sent); NUL can't be stored or shown."""
    values = typed.get(name) or ()
    return values[0].replace("\x00", "") if values else ""


def _listed(text: str) -> tuple[str, ...]:
    """A comma-separated field's entries, blank ones left out."""
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _kind(value: str) -> FactType:
    return FactType.STORY if value == FactType.STORY.value else FactType.FACT


def _privacy(chosen: str) -> tuple[Choice, ...]:
    known = chosen if chosen in PRIVACY_LABELS else Privacy.NORMAL.value
    return tuple(
        Choice(privacy.value, label, explained, privacy.value == known)
        for privacy, label, explained in _PRIVACY
    )


def _wording_fields(fact_type: FactType) -> tuple[EditField, ...]:
    return tuple(
        EditField(name, label, "", _HINTS.get(name, "")) for name, label in _WORDING[fact_type]
    )


def _hinted(fields: Sequence[EditField]) -> tuple[EditField, ...]:
    return tuple(replace(field, hint=_HINTS.get(field.name, "")) for field in fields)


def _refuse(field: str, message: str, refused: bool) -> None:
    if refused:
        raise FactInputInvalid(field, message)


def _capped(
    employer: str, title: str, ticked: Sequence[str], added: Sequence[str], tags: Sequence[str]
) -> None:
    """Refuse what is longer than a fact takes, each on the field it was typed in. Ticked
    skills are the list's own names: only how many there are is counted."""
    _refuse("new_role_employer", _EMPLOYER_TOO_LONG, len(employer.strip()) > MAX_ROLE_CHARS)
    _refuse("new_role_title", _TITLE_TOO_LONG, len(title.strip()) > MAX_ROLE_CHARS)
    _refuse("new_skills", _SKILL_TOO_LONG, any(len(name) > MAX_NAME_CHARS for name in added))
    _refuse("new_skills", _TOO_MANY_SKILLS, len(ticked) + len(added) > MAX_SKILLS)
    _refuse("tags", _TAG_TOO_LONG, any(len(tag) > MAX_NAME_CHARS for tag in tags))
    _refuse("tags", _TOO_MANY_TAGS, len(tags) > MAX_TAGS)


def _input(fact_type: FactType, wording: Mapping[str, str | None], typed: Typed) -> FactInput:
    """What a posted form asks to save: ``wording`` by part, and the details every form gives
    the same way. A role is new when an employer or a title was typed. Raises
    ``FactInputInvalid`` for a name or a list longer than a fact takes."""
    employer, title = one(typed, "new_role_employer"), one(typed, "new_role_title")
    typed_role = bool(employer.strip() or title.strip())
    ticked = [value.replace("\x00", "") for value in typed.get("skills", ())]
    added, tags = _listed(one(typed, "new_skills")), _listed(one(typed, "tags"))
    _capped(employer, title, ticked, added, tags)
    return FactInput(
        type=fact_type,
        text=wording.get("text"),
        situation=wording.get("situation"),
        action=wording.get("action"),
        result=wording.get("result"),
        role_id=one(typed, "role_id") or None,
        new_role=NewRole(employer, title) if typed_role else None,
        skills=(*ticked, *added),
        tags=tags,
        start_date=one(typed, "start_date"),
        end_date=one(typed, "end_date"),
        # A form that sends no privacy field leaves the choice as it is.
        privacy=one(typed, "privacy") if "privacy" in typed else None,
    )


class FactEditor:
    """``bank`` answers the open workspace's fact services (None while none is open)."""

    def __init__(
        self,
        *,
        bank: Callable[[], FactBank | None],
        on_created: Callable[[], None] = lambda: None,
    ) -> None:
        """``on_created`` is told after each new fact is saved (wiring marks setup's CV
        import step done with it, M3.7); it never raises."""
        self._bank = bank
        self._on_created = on_created

    # --- showing --------------------------------------------------------------------------------

    def new(
        self,
        kind: str,
        *,
        typed: Typed | None = None,
        error: FactInputInvalid | None = None,
        note: NoteView | None = None,
    ) -> EditorView:
        """The form for a new fact or story (anything else is a fact), empty or, after a
        refusal, with what was typed. ``note`` (an open capture note) makes it a fact with
        the note's text typed in, and names the note in the form."""
        bank = open_bank(self._bank)
        fact_type = FactType.FACT if note is not None else _kind(kind)
        fields = _wording_fields(fact_type)
        if note is not None:
            fields = tuple(
                replace(field, value=note.text) if field.name == "text" else field
                for field in fields
            )
        view = EditorView(
            fact_id=None,
            kind=fact_type.value,
            heading=_NEW_HEADINGS[fact_type],
            action="/facts/new",
            version=None,
            status=None,
            fields=fields,
            roles=self._roles(bank, None),
            role_id="",
            new_role_employer="",
            new_role_title="",
            skills=tuple(SkillTick(skill.name, False) for skill in bank.skills.list()),
            new_skills="",
            tags="",
            start_date="",
            end_date="",
            privacy=_privacy(Privacy.NORMAL.value),
            intro=_STORY_INTRO if fact_type is FactType.STORY else None,
            note_id=note.id if note is not None else _note_id(typed),
        )
        return _shown_back(view, typed, error)

    def edit(
        self,
        fact_id: str,
        *,
        typed: Typed | None = None,
        error: FactInputInvalid | None = None,
        version: int | None = None,
    ) -> EditorView:
        """The form for one fact as it is stored or, after a refusal, with what was typed and
        the ``version`` that form carried. A discarded fact has no fields: it is shown as a
        card, with what a refused save typed (``kept``) to be copied. A draft comes with
        what made it (``draft``). Raises ``FactNotFound``."""
        bank = open_bank(self._bank)
        fact = bank.facts.get(fact_id)
        editable = fact.status is not FactStatus.DISCARDED
        is_draft = fact.status is FactStatus.DRAFT
        back_url, back_label = _BACK_DRAFTS if is_draft else _BACK_LIBRARY
        view = EditorView(
            fact_id=fact.id,
            kind=fact.type.value,
            heading=_heading(fact),
            action=f"/facts/{fact.id}/edit",
            version=fact.version if version is None else version,
            status=fact.status.value,
            fields=_hinted(edit_fields(fact)) if editable else (),
            roles=self._roles(bank, fact.role_id),
            role_id=fact.role_id or "",
            new_role_employer="",
            new_role_title="",
            skills=tuple(
                SkillTick(skill.name, skill.name in fact.skills) for skill in bank.skills.list()
            ),
            new_skills="",
            tags=", ".join(fact.tags),
            start_date=fact.start_date or "",
            end_date=fact.end_date or "",
            privacy=_privacy(fact.privacy.value),
            item=None if editable else fact_item(fact, role_labels(bank)),
            merged=fact.merged_into_fact_id is not None,
            intro=_STORY_INTRO if editable and fact.type is FactType.STORY else None,
            draft=self._draft(bank, fact) if is_draft else None,
            hints=quality_hints(fact.type, fact.content, fact.status, fact.privacy),
            back_url=back_url,
            back_label=back_label,
            kept=() if editable or typed is None else _kept(fact, typed),
        )
        return _shown_back(view, typed, error) if editable else view

    def _draft(self, bank: FactBank, fact: FactView) -> FactItem:
        """The draft as the Drafts tab shows it: its badges, its flag and the CV line."""
        with bank.db.read() as conn:
            quote = facts_repo.source_quote(conn, fact.id)
        return fact_item(fact, {}, quote)

    def _roles(self, bank: FactBank, own_role_id: str | None) -> tuple[Option, ...]:
        """The roles that can be picked: confirmed ones, and the fact's own draft role."""
        return tuple(
            Option(role.id, role_label(role))
            for role in bank.roles.list()
            if role.status is RoleStatus.CONFIRMED or role.id == own_role_id
        )

    # --- saving ---------------------------------------------------------------------------------

    def create(self, typed: Typed) -> FactView:
        """Save the new-item form: a confirmed fact, with its role and skills, or nothing.
        A ``note`` field naming an open capture note records the fact on that note in the
        same save. Raises ``FactInputInvalid``."""
        bank = open_bank(self._bank)
        fact_type = _kind(one(typed, "type"))
        wording = {name: one(typed, name) for name, _label in _WORDING[fact_type]}
        made = bank.facts.create(_input(fact_type, wording, typed), note_id=_note_id(typed))
        self._on_created()
        return made

    def save(self, fact_id: str, typed: Typed, *, expected_version: int) -> FactView:
        """Save the edit form of one fact. Its type stays; a wording field the form doesn't
        show, or didn't send, keeps what is stored. The version check covers the read made
        here. Raises ``FactNotFound``, ``VersionConflict``, ``FactTransitionRefused`` and
        ``FactInputInvalid``."""
        bank = open_bank(self._bank)
        fact = bank.facts.get(fact_id)
        content = fact.content
        wording: dict[str, str | None] = {
            "text": content.text,
            "situation": content.situation,
            "action": content.action,
            "result": content.result,
        }
        for field in edit_fields(fact):
            if field.name in typed:
                wording[field.name] = one(typed, field.name)
        data = _input(fact.type, wording, typed)
        return bank.facts.edit(fact_id, data, expected_version=expected_version)


def _note_id(typed: Typed | None) -> str | None:
    """The capture note a posted new-item form names, when it names one of the safe shape."""
    if typed is None:
        return None
    note_id = one(typed, NOTE_FIELD)
    return note_id if is_safe_id(note_id) else None


def added_message(made: FactView) -> str:
    """What the save of a new item says: a story is called one."""
    return MSG_STORY_ADDED if made.type is FactType.STORY else MSG_ADDED


def saved_message(saved: FactView) -> str:
    """What a save says. A draft stays a draft, and the line says so."""
    if saved.status is FactStatus.DRAFT:
        return MSG_DRAFT_SAVED
    return MSG_STORY_SAVED if saved.type is FactType.STORY else MSG_SAVED


def _heading(fact: FactView) -> str:
    if fact.status is not FactStatus.DISCARDED:
        return _EDIT_HEADINGS[fact.type]
    return _HEADING_MERGED if fact.merged_into_fact_id is not None else _HEADING_DISCARDED


def _kept(fact: FactView, typed: Typed) -> tuple[EditField, ...]:
    """What a save typed for a fact that was discarded meanwhile: its wording fields and the
    other typed-in fields that hold something, each to be shown read-only."""
    wording = tuple(
        EditField(field.name, field.label, one(typed, field.name))
        for field in edit_fields(fact)
        if field.name in typed
    )
    others = tuple(
        EditField(name, label, one(typed, name))
        for name, label in _KEPT_LABELS
        if one(typed, name).strip()
    )
    return wording + others


def stale(view: EditorView) -> EditorView:
    """The form after a stale save: what was typed stays to be copied, but it carries a
    version that never saves."""
    return replace(view, version=_NO_VERSION)


def _shown_back(
    view: EditorView, typed: Typed | None, error: FactInputInvalid | None
) -> EditorView:
    """``view`` with what a refused form held, and the refusal tied to one of its fields (the
    first wording field when the error names none the form has)."""
    if typed is None:
        return view
    ticked = set(typed.get("skills", ()))
    role_id = one(typed, "role_id")
    shown = replace(
        view,
        fields=tuple(
            replace(field, value=one(typed, field.name)) if field.name in typed else field
            for field in view.fields
        ),
        role_id=role_id if any(option.value == role_id for option in view.roles) else "",
        new_role_employer=one(typed, "new_role_employer"),
        new_role_title=one(typed, "new_role_title"),
        skills=tuple(SkillTick(tick.name, tick.name in ticked) for tick in view.skills),
        new_skills=one(typed, "new_skills"),
        tags=one(typed, "tags"),
        start_date=one(typed, "start_date"),
        end_date=one(typed, "end_date"),
        privacy=_privacy(one(typed, "privacy")) if "privacy" in typed else view.privacy,
    )
    if error is None:
        return shown
    names = [field.name for field in view.fields]
    known = error.field in names or error.field in _OTHER_FIELDS
    return replace(
        shown,
        error_field=error.field if known else names[0],
        error_message=error.user_message,
    )
