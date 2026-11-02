"""The running app, assumed interface, markup hooks and readers for the M8.3 part B tests of the
company brief (``company_brief``: an on-demand, web-searched, sourced summary of a company).
Every value is fictional. No part B application module is imported at module level, so a missing
service or route fails the test that needs it, not the collection.

ASSUMED INTERFACE (the implementer follows it; spec 3.6, 3.7, ADR-124 items 2 to 4 and 7,
ADR-128, ADR-129)

Task and wiring
- Prompt ``prompts/company_brief`` v1 and ``TaskSpec("company_brief", kind="summary",
  allow_web_search=True, max_searches=5)`` (see
  ``tests/unit/modules/ai/test_company_brief_prompt.py``).
  Task type ``company_brief``, label "Company brief" (the tasks indicator says "Company brief
  finished."), dedupe key ``company_brief:<company id>``. The task row holds ids and the tier
  only, never any text. The request has ONE untrusted block: a JSON object with keys
  ``company``, ``website`` and ``industry`` (the company's stored values), whose ``id`` attribute
  is the company id. Nothing else about the person, a job, a CV or a fact is an input.
- ``app.state.company_brief`` is a ``BriefService`` (``app/services/company_brief.py``), built like
  ``PrepService`` (same constructor keywords: ``factory``, ``clock``, ``ids``, ``workspace``,
  ``settings``, ``tasks``, ``usage``, ``search``, ``search_settings``, ``transient``, ``tz``; the
  search policy callable is kept as ``_search`` and the bounded store as ``_transient``). Unknown
  or unsafe company ids raise ``LookupError`` (as ``CompaniesService``).

``BriefService`` (blocking)
- ``view(company_id) -> BriefView`` reads only (nothing is written, started or requested):
  ``company_id``; ``company`` (the name); ``version`` (the company's ``version``);
  ``brief`` (the LATEST SAVED brief, else None; older rows are kept but never shown);
  ``transient`` (a brief held only in memory, else None); ``storage_notice`` ("" or one of the two
  texts below); ``search_provider`` (the search policy or None); ``tasks`` (a tuple of
  ``(type, status, error)`` of the latest ``company_brief`` task, as ``PrepView.tasks``).
  A brief (``brief`` / ``transient``) has ``sections`` (a tuple of ``BriefSection``), ``sources``
  (a tuple of urls) and ``found_at`` (an ISO instant).
- ``plan_start(company_id, *, tier=None) -> SummaryPlan | None``: the AI service and tier the
  route asks the budget gate; None when no search provider is available.
- ``start(company_id, *, version, tier=None) -> TaskRef``: ``VersionConflict`` for a stale
  company ``version``; ``ProviderNotConfigured`` without a search provider; enqueues
  ``company_brief`` (dedupe key above). It makes no request itself.
- The run (in the task) sends ONE request through the runner with the meter; every web search the
  provider asks for is counted by the ``web_search`` meter (the executor's reserve refuses past
  the budget or the five-search cap). The answer goes through ``resolve_brief`` (searched urls
  only). A result with no kept section is NOT saved and not held. Before writing, the company's
  name, website and industry are re-read (as ``PrepService.run`` does in ``write``); if they
  changed since the run loaded them, nothing is saved and the task fails. As in
  ``PrepService.run``, the company is loaded first and the search policy is read afterwards.
- Storage rule (ADR-129): the search policy's ``storage_allowed`` True: one row in
  ``company_briefs`` (id, company_id, ``content`` = ``{"sections": [{"heading", "text",
  "source_url", "source_date"}, ...]}``, ``sources`` = the JSON array of the kept urls,
  ``found_at`` = the clock's instant, provider, model, ``prompt_id`` ``company_brief``,
  ``prompt_version`` ``1``, created_at, updated_at, version 1), written in the transaction that
  marks the task succeeded. None or False: NOTHING derived is written to any table; the result
  is held in the bounded ``TransientSearchResults`` under the key ``"brief:" + company_id`` and
  ``view`` shows it with ``storage_notice`` "Not saved: this search provider's terms on keeping
  results are unclear" (None) or "Not saved: this search provider does not allow keeping
  results" (False). A new run replaces what is held.

Routes (new ``app/api/company_brief.py``; token on both; the company page itself stays token-free)
- ``GET /companies/{id}/brief``: the ``#company-brief`` fragment (200); an unknown or unsafe id is
  the fixed plain-text 404 "This company wasn't found.". It writes nothing and makes no request.
- ``POST /companies/{id}/brief`` (token and origin): fields ``version`` (the company's, an integer
  of at least 1; anything else, a missing one or an unknown field is 400) plus the budget fields
  of the other starts (``tier``, ``budget_chosen_tier``, ``budget_service``, ``budget_threshold``,
  ``budget_cycle_start``). 200 started (the fragment, header ``HX-Trigger`` announce "Company
  brief started.", focus on ``#company-brief-heading`` via ``data-autofocus``) or 200 with the
  budget warning dialog and nothing started; 409 for a stale version (the fragment with the
  reload prompt), a blocked budget (the reason, nothing queued) or no search provider; 404 for an
  unknown company. The budget check runs in the route, before anything is queued: the AI
  service (look-ahead 1) and ``web_search`` (look-ahead 5).

Markup (fragment ``fragments/companies/_brief.html``; the same fragment is included on the
company page between History and the edit form, and in the Prep tab's Research section)
- Root ``<section id="company-brief" aria-labelledby="company-brief-heading">`` with ``hx-get`` to
  ``/companies/{id}/brief`` and ``hx-trigger`` holding ``tasks:updated from:body`` and
  ``usage:changed from:body`` (and ``every 2s`` while a task is active); heading
  ``#company-brief-heading``.
- A form posting (``hx-post``, ``hx-target="#company-brief"``) to ``/companies/{id}/brief`` with a
  hidden ``version`` and the button ``#company-brief-start`` whose text is "Get a company brief"
  (no brief yet) or "Refresh the brief" (a saved or held one is shown). Without a search provider
  there is no button and the text says a brief "needs a search provider" with a Settings link.
- ``#company-brief-use``: states AI and web search are used, up to five searches per attempt, and
  "This month: N search(es) of M" (as the web questions line of the Prep tab).
- Blocked: the shared ``fragments/budget/_blocked.html`` with reason id ``company-brief-reason``
  ("Monthly search limit reached. Resets on <date>." plus the Settings link
  ``/settings#usage-budgets``) and the button ``aria-disabled="true"
  aria-describedby="company-brief-reason"``; the rest of the page keeps working.
- Sections: each in an element with ``data-brief-section="<heading key>"`` holding the heading
  label (What they do, Products, Size and location, Recent news, Culture, Other), the text and
  one link ``<a href="<source_url>" rel="noopener noreferrer nofollow">``; a source date shows
  only as "<date> (as stated by the source)". Source urls are never fetched by the app.
- ``#company-brief-found`` says "Found <d month yyyy>" in the app time zone for a saved brief.
- ``#company-brief-notice`` (class notice) starts with the storage notice for a held brief.
- ``#company-brief-working`` (role status) while a task is active; the button is then
  ``aria-disabled``.
- Budget dialog: ``company-brief-budget-dialog`` (``data-modal``, ``data-return-focus``
  ``#company-brief-start``), posting to the same route and replacing ``#company-brief``.
"""

import json
import re
from typing import Any

from app.modules.ai import AICapabilities
from app.modules.ai.providers.fake import ScriptedResponse
from tests.board_pages import elements
from tests.contact_pages import company_id_of
from tests.fakes.search import FakeSearchProvider
from tests.tailoring_pages import Tailoring

COMPANY = "Harbor Kite Fictional"  # the company of every Tailoring application
WEBSITE = "https://harborkite.example.test"
INDUSTRY = "Logistics software"
TASK = "company_brief"
NOT_FOUND = "This company wasn't found."
STARTED = "Company brief started."
UNCLEAR = "Not saved: this search provider's terms on keeping results are unclear"
FORBIDDEN = "Not saved: this search provider does not allow keeping results"
SOURCE = "https://example.test/briefsourceheron"
SOURCE_2 = "https://example.test/briefnewsplover"
UNSEARCHED = "https://unsearched.test/page"
# Letters only: a marker with digits could match an id or a timestamp.
ABOUT_MARKER = "briefmarkerkestrel"
ABOUT = f"Plans delivery routes for small depots ({ABOUT_MARKER})."
NEWS = "Opened a second fictional depot this year."
UNSEARCHED_TEXT = "A claim from a page no search returned."
OPEN = "<untrusted_document"
CLOSE = "</untrusted_document>"


def brief_data(*, about: str = ABOUT, extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Two sourced sections and one whose url the search never returned."""
    return {
        "sections": [
            {
                "heading": "what_they_do",
                "text": about,
                "source_url": SOURCE,
                "source_date": "March 2024",
            },
            {"heading": "recent_news", "text": NEWS, "source_url": SOURCE_2, "source_date": None},
            {
                "heading": "culture",
                "text": UNSEARCHED_TEXT,
                "source_url": UNSEARCHED,
                "source_date": None,
            },
            *(extra or []),
        ]
    }


def scripted(data: dict[str, Any] | None = None, *, tool_calls: int = 2) -> ScriptedResponse:
    return ScriptedResponse(
        data=brief_data() if data is None else data,
        tool_calls=tool_calls,
        searched_urls=(SOURCE, SOURCE_2),
    )


def searching(r: Any, storage: bool | None = True) -> Any:
    """Let the provider search and set the search policy of the brief (and prep) service."""
    r.run.spy.capabilities = AICapabilities(web_search=True, reports_token_usage=True)
    for name in ("company_brief", "prep"):
        getattr(r.run.app.state, name)._search = lambda provider: FakeSearchProvider(
            storage_allowed=storage
        )
    return r.run.app.state.company_brief


def company_id(r: Any, name: str = COMPANY) -> str:
    return company_id_of(r.run.app.state.workspace.db, name)


def brief_ready(r: Any, storage: bool | None = True, *, data: dict[str, Any] | None = None):
    """An application (hence a company) with website and industry, a searching provider and a
    scripted answer. Returns ``(application id, company id, service)``."""
    desk = Tailoring(r)
    # One Tailoring numbers the companies it writes, so a test needing a second application must
    # use this same instance (a second one would reuse the first company id).
    r.brief_desk = desk
    aid = desk.ready("brief-job", facts=False).id
    cid = company_id(r)
    r.run.execute("UPDATE companies SET website=?, industry=? WHERE id=?", (WEBSITE, INDUSTRY, cid))
    service = searching(r, storage)
    r.run.spy.script(TASK, scripted(data))
    return aid, cid, service


def company_version(r: Any, cid: str) -> int:
    return int(r.run.rows("SELECT version FROM companies WHERE id=?", (cid,))[0]["version"])


def run_brief(r: Any, service: Any, cid: str, *, status: str = "succeeded") -> Any:
    ref = service.start(cid, version=service.view(cid).version)
    assert r.run.wait_terminal(ref.id).status.value == status
    return ref


def brief_path(cid: str) -> str:
    return f"/companies/{cid}/brief"


def requests(r: Any) -> list[Any]:
    return [q for q in r.run.spy.requests if q.task == TASK]


def sections_of(page: str) -> dict[str, Any]:
    """The rendered sections by heading key."""
    found = {}
    for e in elements(page):
        if "data-brief-section" in e.attrs:
            found[str(e.attrs["data-brief-section"])] = e
    return found


def block_of(user: str, source: str) -> str:
    found = re.search(rf'<untrusted_document source="{source}"[^>]*>\n?(.*?)\n?{CLOSE}', user, re.S)
    assert found is not None, source
    return found.group(1)


def context_of(user: str) -> dict[str, Any]:
    """The JSON of the one untrusted block of a brief request."""
    blocks = re.findall(rf"{OPEN}[^>]*>\n(.*?)\n{CLOSE}", user, re.S)
    assert len(blocks) == 1, len(blocks)
    loaded: dict[str, Any] = json.loads(blocks[0])
    return loaded
