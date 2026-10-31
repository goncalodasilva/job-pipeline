"""Today's "Suggested session" block (M7.8; AC-M7.6; ADR-117 item 8, ADR-119).

``block()`` only reads: it never stores the reset or any default. The reset window is the pure
``suggestion_due`` rule over the ``sessions`` setting, the ``session_suggestion`` setting
(``{"last_reset": <instant>}``, written by ``SessionService.start`` and by ``dismiss``) and the
newest ``applied_at``. A suggestion also needs a saved AI provider and something to ask.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.clock import Clock
from app.domain.enrichment import suggestion_due
from app.domain.errors import SessionNotFound
from app.domain.ids import is_safe_id
from app.domain.settings import AI_PROVIDER, SESSIONS
from app.modules.storage import Database, applications_repo, get_setting, sessions_repo
from app.services.sessions import SessionService
from app.services.settings import SettingsService

SUGGESTION_KEY = "session_suggestion"
# "about N minutes" for a session of 3 or 5 questions (design: Today block 5).
MINUTES = {3: 5, 5: 8}


@dataclass(frozen=True)
class SuggestionBlock:
    """What the block shows: a session to continue, or a suggested one (``length`` set)."""

    open_session_id: str | None = None
    length: int | None = None

    @property
    def minutes(self) -> int | None:
        return None if self.length is None else MINUTES[self.length]


class SessionSuggestions:
    def __init__(
        self, database: Callable[[], Database | None], clock: Clock, sessions: SessionService
    ) -> None:
        self._database = database
        self._clock = clock
        self._sessions = sessions

    def block(self) -> SuggestionBlock | None:
        db = self._database()
        if db is None:
            return None
        now = self._clock.now().astimezone(UTC)
        settings = SettingsService(db, self._clock)
        with db.read() as conn:
            opened = sessions_repo.open_session(conn)
            stored = get_setting(conn, SUGGESTION_KEY)
            applied = applications_repo.latest_applied_at(conn, until=now.isoformat())
        if opened is not None:  # already running: shown whatever the frequency, pause or backlog
            return SuggestionBlock(open_session_id=opened.id) if is_safe_id(opened.id) else None
        sessions = settings.load(SESSIONS).value
        due = suggestion_due(
            sessions, _last_reset(stored.value if stored else None), _instant(applied), now
        )
        if not due or settings.load(AI_PROVIDER).value.kind is None:
            return None
        try:
            if not self._sessions.backlog():
                return None
        except SessionNotFound:
            return None
        return SuggestionBlock(length=sessions.length)

    def dismiss(self) -> None:
        """Start the window now, as starting a session does. Nothing else is written."""
        db = self._database()
        if db is None:
            return
        with db.transaction() as conn:
            SessionService.reset_suggestion(conn, self._clock.now().isoformat())


def _instant(value: str | None) -> datetime | None:
    try:
        return None if value is None else datetime.fromisoformat(value)
    except ValueError:
        return None


def _last_reset(value: str | None) -> datetime | None:
    """The stored ``last_reset``, or None when nothing (usable) is stored."""
    if value is None:
        return None
    try:
        stored = json.loads(value)["last_reset"]
        return datetime.fromisoformat(stored) if isinstance(stored, str) else None
    except (ValueError, KeyError, TypeError):
        return None
