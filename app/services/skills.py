"""The user's own list of skills (ADR-089 item 11, ADR-090 item 9): names are compared after
normalization, a skill may have other spellings (aliases), and the list starts empty."""

import builtins
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from app.domain.clock import Clock
from app.domain.errors import FactInputInvalid, FactNotFound, SkillAliasTaken, VersionConflict
from app.domain.ids import IdFactory
from app.domain.skills import display_skill, normalize_skill, skill_matches
from app.modules.storage import Database, skills_repo

_NAME_NEEDED = "Type a skill name."


@dataclass(frozen=True)
class SkillView:
    id: str
    name: str
    aliases: tuple[str, ...]
    version: int


def _all(conn: sqlite3.Connection) -> builtins.list[SkillView]:
    return [
        SkillView(row.id, row.name, tuple(json.loads(row.aliases)), row.version)
        for row in skills_repo.list_skills(conn)
    ]


def _named(skills: Sequence[SkillView], name: str) -> SkillView | None:
    """The skill ``name`` means, by its name or an alias. The list is the user's own and
    short, so it is read whole and compared in Python, where normalization lives."""
    for skill in skills:
        if skill_matches(name, skill.name, skill.aliases):
            return skill
    return None


def _shown(field: str, name: str) -> str:
    if not normalize_skill(name):
        raise FactInputInvalid(field, _NAME_NEEDED)
    return display_skill(name)


def _insert(
    conn: sqlite3.Connection, skill_id: str, name: str, aliases: tuple[str, ...], now: str
) -> SkillView:
    skills_repo.insert_skill(
        conn,
        skill_id=skill_id,
        name=name,
        normalized_name=normalize_skill(name),
        aliases=json.dumps(aliases),
        now=now,
    )
    return SkillView(skill_id, name, aliases, 1)


def resolve_skills(
    conn: sqlite3.Connection, names: Iterable[str], *, ids: IdFactory, now: str
) -> tuple[str, ...]:
    """The ids of the skills ``names`` mean, each once, in the caller's transaction. A name
    the list doesn't have yet (as a name or an alias) is added to it; blank names are left
    out."""
    known = _all(conn)
    found: dict[str, None] = {}
    for name in names:
        if not normalize_skill(name):
            continue
        skill = _named(known, name)
        if skill is None:
            skill = _insert(conn, ids.new(), display_skill(name), (), now)
            known.append(skill)
        found[skill.id] = None
    return tuple(found)


class SkillService:
    def __init__(self, db: Database, clock: Clock, ids: IdFactory) -> None:
        self._db = db
        self._clock = clock
        self._ids = ids

    def list(self) -> tuple[SkillView, ...]:
        """Every skill, in alphabetical order."""
        with self._db.read() as conn:
            return tuple(_all(conn))

    def find(self, name: str) -> SkillView | None:
        """The skill with this name or alias, if any."""
        with self._db.read() as conn:
            return _named(_all(conn), name)

    def add(self, name: str, *, aliases: Sequence[str] = ()) -> SkillView:
        """Add a skill, or return the one that already has this name or alias (unchanged).
        Raises ``SkillAliasTaken`` when a new skill's alias means another skill."""
        shown = _shown("name", name)
        spellings: dict[str, str] = {}
        for alias in aliases:
            spelling = _shown("aliases", alias)
            if not skill_matches(spelling, shown, spellings.values()):
                spellings[normalize_skill(spelling)] = spelling
        now = self._clock.now().isoformat()
        with self._db.transaction() as conn:
            known = _all(conn)
            existing = _named(known, shown)
            if existing is not None:
                return existing
            if any(_named(known, spelling) is not None for spelling in spellings.values()):
                raise SkillAliasTaken(detail="an alias of the new skill names another skill")
            return _insert(conn, self._ids.new(), shown, tuple(spellings.values()), now)

    def add_alias(self, skill_id: str, alias: str, *, expected_version: int) -> SkillView:
        """Give a skill another spelling. Raises ``SkillAliasTaken`` when it means another
        skill; a spelling the skill already has changes nothing."""
        shown = _shown("alias", alias)
        now = self._clock.now().isoformat()
        with self._db.transaction() as conn:
            known = _all(conn)
            skill = next((found for found in known if found.id == skill_id), None)
            if skill is None:
                raise FactNotFound(detail=f"skill {skill_id} not found")
            owner = _named(known, shown)
            if owner is not None and owner.id != skill.id:
                raise SkillAliasTaken(detail=f"the alias names skill {owner.id}")
            if owner is not None:
                return skill
            aliases = (*skill.aliases, shown)
            if not skills_repo.set_aliases(
                conn, skill.id, json.dumps(aliases), expected_version=expected_version, now=now
            ):
                raise VersionConflict(detail=f"skill {skill_id} changed elsewhere")
            return replace(skill, aliases=aliases, version=expected_version + 1)
