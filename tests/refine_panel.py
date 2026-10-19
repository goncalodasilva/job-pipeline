"""The Refine with AI panel's contract (M3.9; AC-M3.5 UI; spec 3.1 item 5 as updated on
2026-10-19; ADR-097 and its update; detailed design, "Refine with AI").

Everything the integration and end-to-end tests pin about the panel is named here once.
The routes of M3.8 (``app/api/facts_refine.py``, ``tests/refine_harness.py``) stay as they
are; M3.9 adds one fragment route and the panel on the fact editor page:

- ``GET /facts/{id}/refine``: the ``#fact-refine`` fragment (launch token needed, not a page,
  no side effects: no conversation, task or request is made). Unknown fact: the fixed 404.
- ``GET /facts/{id}/edit`` (a page): the editor holds ``#fact-refine``, as the fragment shows
  it, so a reload keeps an open conversation.

What the panel shows:

- No open conversation: a "Refine with AI" button in a form that posts to
  ``/facts/{id}/refine`` (``action`` and ``hx-post`` equal) with ``hx-target="#fact-refine"``.
  For a "never use" fact the same button is blocked (ADR-086): focusable, ``aria-disabled=
  "true"`` and never the ``disabled`` attribute, ``aria-describedby`` naming the visible
  reason, which is ``RefineNeverUse``'s message. Pressing it sends nothing.
- A turn queued or running: the waiting line (``WAITING``), a "Cancel" button whose form posts
  to ``/facts/{id}/refine/discard``, and "Edit it myself"; no answer box and no Accept. While
  it waits, the panel (or an element in it) refetches ``GET /facts/{id}/refine`` on the
  ``tasks:updated`` and ``tasks:resync`` body events. Once the AI has answered, nothing in the
  panel refetches on those events, so an answer being typed is never replaced.
- The AI answered: its questions (``#fact-refine-questions``) and, while answers are left,
  an answer box (a labelled ``textarea name="answer"`` in a form posting to
  ``/facts/{id}/refine/answer``, sent with "Send answer"); the suggested wording as a diff
  against the fact's current wording in ``#fact-refine-suggestion``, written as the history
  writes one (``<del>``/``<ins>`` runs that start with "removed"/"added", hidden from sight
  only); "Practice data (fake provider)" when the fake answered.
- Accept form (posts to ``/facts/{id}/refine/accept``): the fact's version as a hidden
  ``version``, the suggested wording in a labelled, editable ``textarea name="text"``, and in
  ``#fact-refine-flagged`` the line "This isn't in your fact bank." with, per flagged claim,
  a checkbox ``name="acknowledged" value="<claim>"`` labelled "This is true; add it" and tied
  to the claim it is about. The Accept button is ``aria-disabled="true"`` (never
  ``disabled``), described by a visible reason, until each claim is ticked or edited out of
  the wording; while it is, pressing it sends nothing.
- "Edit it myself" (a link or a button) in every open state: it takes focus to the editor's
  wording field (``#editor-text``). "Discard" (a button whose form posts to
  ``/facts/{id}/refine/discard``) once the AI has answered.
- The live region says ``SAID_READY`` each time a turn's answer (a suggestion) appears.

Everything is fictional.
"""

from typing import Final

from app.modules.ai.providers.fake_scripts import (
    KEYWORD_REFINE_INVENTED,
    KEYWORD_REFINE_SLOW,
    KEYWORD_REFINE_USES_ANSWER,
)
from tests.refine_harness import TARGET

# --- ids, names and copy ----------------------------------------------------------------------

PANEL_ID: Final = "fact-refine"
PANEL_TARGET: Final = f"#{PANEL_ID}"
PANEL_HEADING_ID: Final = "fact-refine-heading"
QUESTIONS_ID: Final = "fact-refine-questions"
SUGGESTION_ID: Final = "fact-refine-suggestion"
FLAGGED_ID: Final = "fact-refine-flagged"
EDITOR_WORDING_ID: Final = "editor-text"  # the editor's own wording field (M3.6)

REFINE_BUTTON: Final = "Refine with AI"
SEND_ANSWER: Final = "Send answer"
ACCEPT: Final = "Accept"
EDIT_MYSELF: Final = "Edit it myself"
DISCARD: Final = "Discard"
CANCEL: Final = "Cancel"

ANSWER_FIELD: Final = "answer"
WORDING_FIELD: Final = "text"
ACKNOWLEDGED_FIELD: Final = "acknowledged"
VERSION_FIELD: Final = "version"

NOT_IN_BANK: Final = "This isn't in your fact bank."
TRUE_ADD: Final = "This is true; add it"
PRACTICE_BADGE: Final = "Practice data (fake provider)"
REFINED_SOURCE: Final = "Refined with AI, accepted by you"  # the history's label (M3.8)

SAID_STARTED: Final = "The AI is reading your fact."  # facts_refine.MSG_STARTED
SAID_ANSWERED: Final = "Your answer was sent."
SAID_ACCEPTED: Final = "The wording was accepted."
SAID_DISCARDED: Final = "The suggestion was discarded. Your fact is unchanged."
SAID_READY: Final = "The AI's suggestion is ready."
WAITING: Final = SAID_STARTED  # the waiting line, on the POST answer and on a refetch

# --- review round (M3.9) -------------------------------------------------------------------------
# Cancel and "Edit it myself" keep their ids, so a refresh never drops focus from them; a
# refetch whose phase didn't change answers 204 (nothing swapped).
CANCEL_ID: Final = "fact-refine-cancel"
EDIT_ID: Final = "fact-refine-edit"
STATUS_ID: Final = "fact-refine-status"
WAS_FIELD: Final = "was"
CANCEL_HINT: Final = "This ends the conversation."
SAID_STOPPED: Final = "Stopped. Your fact is unchanged."  # Cancel before any suggestion
SAID_QUESTIONS_READY: Final = "The AI's questions are ready."
SAID_FAILED: Final = "The AI couldn't answer this time. Your fact is unchanged."
TRY_AGAIN: Final = "Try again"
TRY_FROM_TASKS: Final = "Try again from Tasks, or discard and start over."
STALE_NOTE: Final = (
    "Your fact changed after this suggestion. Discard it and ask again, or keep editing it "
    "yourself."
)
# A fact whose turn the fake answers only after a few seconds (a visible waiting state).
SLOW_FACT: Final = f"{TARGET[:-1]} ({KEYWORD_REFINE_SLOW})."
ANSWERS_USED: Final = "You've used your three answers: accept, edit it yourself or discard."

DIFF_ADDED: Final = "added"
DIFF_REMOVED: Final = "removed"
REFRESH_EVENTS: Final = ("tasks:updated from:body", "tasks:resync from:body")

# --- the fake's fictional answers (app/modules/ai/providers/fake_scripts.py) -------------------

QUESTION: Final = "About how many invoices did the team handle each month?"
OK_SUGGESTION: Final = "Reduced invoice processing time by 35% across 4 warehouses."
INVENTED: Final = "Cut invoice processing time by 85% across 14 warehouses at Globex Corporation."
USES_ANSWER: Final = (
    "Cut invoice processing time by 35% across 4 warehouses, handling about 1200 invoices a month."
)
# The claims of INVENTED the honesty check flags (tests/integration/test_fact_refine.py).
INVENTED_NUMBERS: Final = frozenset({"85%", "14"})
INVENTED_NAME: Final = "Globex"

# A fact whose wording asks the fake for the invented answer; its own words are TARGET's.
INVENTED_FACT: Final = f"{TARGET[:-1]} ({KEYWORD_REFINE_INVENTED})."
# An answer that asks the fake for the suggestion using the number it gives.
ANSWER_1200: Final = f"About 1200 invoices a month ({KEYWORD_REFINE_USES_ANSWER})."
