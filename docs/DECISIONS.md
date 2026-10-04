# Decisions

Architecture decision records, newest last. Each entry: context, decision, consequences. Add a new entry instead of editing an old one; mark superseded entries.

## ADR-001 — The user always submits applications (2026-10-03)

- Context: automating applications risks the user's reputation and control.
- Decision: the app prepares everything but never submits applications or automates employer sites.
- Consequences: no browser automation of job sites; "Mark as applied" is a manual step.

## ADR-002 — Local-first, single-user web app (2026-10-03)

- Decision: one local process serving a browser UI on 127.0.0.1; data in a data folder outside the repository.
- Consequences: host, token and origin checks; no hosted mode; network access from a phone stays off by default.

## ADR-003 — Python with server-rendered pages and htmx (2026-10-03)

- Context: the user chose Python and htmx; performance matters.
- Decision: FastAPI (suggested) with Jinja2 templates and htmx; small vendored scripts for drag-and-drop (SortableJS), shortcuts and the command palette.
- Consequences: no frontend build step; focus management and announcements after swaps are explicit work.

## ADR-004 — SQLite with plain sqlite3 and SQL migrations (2026-10-03)

- Decision: standard library `sqlite3`, thin repositories, forward-only numbered SQL migrations, automatic backup before migrating.
- Consequences: no ORM; schema-equality test between fresh and upgraded databases.

## ADR-005 — AI behind a task-level module; Claude Agent SDK first (2026-10-03)

- Decision: services call task functions (`summarize_job`, `tailor_cv`...); providers implement a protocol; the first provider uses the Claude Agent SDK with a subscription token or an API key.
- Consequences: swapping providers touches one package; the app informs users that Anthropic's terms ask developers building products or services with the Agent SDK to use API keys.

## ADR-006 — Honesty enforced in code (2026-10-03)

- Decision: generated content must cite confirmed fact ids; an honesty checker flags uncited content and unsupported numbers; flagged content blocks approval.
- Consequences: output schemas carry `fact_ids`; adversarial fixtures are mandatory for generation tasks.

## ADR-007 — Secrets in the OS keychain (2026-10-03)

- Decision: `keyring` storage, environment-variable fallback, never in the database, logs, responses or exports.

## ADR-008 — Optional usage budgets (2026-10-03)

- Decision: per external provider, user-defined allowance, two warnings, blocking point and reset day; optional per-tier caps; the higher of local and provider-reported counts wins.

## ADR-009 — Web search through the Agent SDK only, for now (2026-10-03)

- Decision: the search module's first provider delegates to the AI provider's built-in web search; other providers can be added later.

## ADR-010 — Send all confirmed facts while the bank is small (2026-10-03)

- Decision: AI requests include all confirmed facts; switch to search-selected facts when size or usage requires, without changing storage.

## ADR-011 — Seen and snooze instead of a Later button (2026-10-03)

- Decision: a card counts as seen once shown; moving on replaces "Later"; snooze lives in the card menu.

## ADR-012 — Fit bands describe evidence, not interest (2026-10-03)

- Decision: Strong, Possible and Stretch are kept; bands never hide a job; deal-breakers are flagged, not hidden, unless the user enables auto-hide.

## ADR-013 — In-app reminders only (2026-10-03)

- Decision: reminders appear when the app is open; nothing runs in the background.

## ADR-014 — PDF and Word exports with country templates (2026-10-03)

- Decision: both formats; the AI drafts a template per country, the user approves it once, and a fixed renderer produces documents. Renderer library chosen by a spike at the start of M6.

## ADR-015 — Responsive layouts (2026-10-03)

- Decision: every screen works down to phone width.

## ADR-016 — Voice on demand, only when supported (2026-10-03)

- Decision: speech-to-text and text-to-speech are optional provider capabilities; controls are hidden when unsupported.

## ADR-017 — MIT license (2026-10-03)

- Decision: MIT, for the widest reuse. Revisit (Apache 2.0 or AGPL) before publishing if patent grants or copyleft matter.

## ADR-018 — Setup guide as its own module (2026-10-03)

- Decision: `app/onboarding/` with a step registry, triggered by first-run detection (bootstrap config, database, setup state, provider health) and re-runnable with `--setup` or from Settings.

## ADR-019 — Calendar behind its own module (2026-10-03)

- Decision: .ics export first; future sync providers store external event ids.

## ADR-020 — Fetch module with an SSRF guard (2026-10-03)

- Context: the technical spec added a dedicated module for user-supplied job URLs.
- Decision: http and https only, no private or local addresses (checked after redirects), size and time limits, no crawling.

## ADR-021 — Built with a Claude Code agentic system (2026-10-03)

- Decision: one orchestrating session, ten project subagents, workflow and procedure skills, hooks and permission rules, with progress and decisions kept in `docs/` (spec section 6).

## ADR-022 — Supported Python versions and accessibility test tooling (2026-10-03)

- Context: CI tests the oldest and newest supported Python (spec 4.6). Python 3.15.0 has only just been released, and binary wheels (for example pydantic-core) may lag. The e2e suite needs axe-core.
- Decision: support Python 3.11 to 3.14; CI tests 3.11 and 3.14. Revisit 3.15 at M9. axe-core is vendored under `tests/e2e/vendor/` with its MPL-2.0 license, instead of adding a dev dependency.
- Consequences: `requires-python = ">=3.11"`; updating axe-core is a manual, reviewed file change.

## ADR-023 — `/health` is exempt from the launch token (2026-10-03)

- Context: spec 2.9 requires the launch token on every JSON endpoint, but AC-M0.1, the smoke test and the CLI's browser-opening probe call `/health` without one.
- Decision: `GET /health` needs no launch token; the host check still applies. It returns only the app version, database status and task runner status, never personal data. It responds 503 when the database is not usable.
- Consequences: spec 2.9 updated. Static files and full-page GETs are the only other token-free routes.

## ADR-024 — `JOBPIPELINE_CONFIG_DIR` overrides the config folder (2026-10-03)

- Context: subprocess tests (smoke, e2e) would otherwise read the developer's real platform config folder, which the privacy rules forbid. On Windows, platformdirs ignores environment overrides.
- Decision: add `JOBPIPELINE_CONFIG_DIR`, which replaces `platformdirs.user_config_dir("jobpipeline")` when set. Precedence is unchanged otherwise.
- Consequences: spec 7.3 updated. Every test that starts the app sets both the data and the config folder to temporary folders.

## ADR-025 — Demo mode folder and config defaults (2026-10-03)

- Decision: demo mode uses `platformdirs.user_data_dir("jobpipeline-demo")`, a sibling of the real data folder, and ignores the bootstrap config's `data_dir`. An explicit `--data-dir` wins over `--demo`; `JOBPIPELINE_DATA_DIR` beats both. Bootstrap config `data_dir` must be an absolute path (with `~` expanded). The bootstrap file `config.toml` is always parsed, so a malformed file is reported even with `--demo` or `--data-dir`. Log levels are `DEBUG`, `INFO`, `WARNING`, `ERROR` (case-insensitive on input, uppercase in runtime config). Defaults are port 8000 and log level INFO. M0 only resolves the path; demo data seeding is M9.

## ADR-026 — Module interfaces in M0, implementations in their milestones (2026-10-03)

- Context: the M0 scope lists the module protocols from spec 2.4, and the M1 scope lists "AI module core: provider protocol".
- Decision: M0 adds the interface types only (protocols and dataclasses for ai, search, calendar, secrets, fetch; the storage `Database` comes with M0.11). Implementations, fake providers and contract suites arrive in the milestone that needs them, which may refine the types.
- Open: spec 4.2 puts `FakeAIProvider` and `MemorySecretStore` in `tests/fakes/`, but the running app must be able to select the fake provider, and spec 2.13 lists a memory store under `secrets/providers/`. Settle where app-selectable fakes live when planning M1. M0's only fakes are `FrozenClock` and `SequentialIdFactory`.

## ADR-027 — Migration 0001 contents (2026-10-03)

- Context: the detailed design's columns for `settings`, `setup_state`, `tasks` and `usage_records` lack the `created_at`, `updated_at` and `version` columns that spec 2.7 requires on mutable tables. The spec takes precedence.
- Decision: every mutable table in `0001` carries `created_at`, `updated_at` and `version`. `0001` has no full-text search tables (their source tables arrive in M3 and M4); M0 only tests that SQLite supports FTS5 and JSON. The unique partial index on active `tasks.dedupe_key` and the `usage_records.task_id` link are added by an M2 migration.
- Consequences: spec section 5 (M0 and M2 scope) updated.
- Details (M0.12): CHECK constraints on tasks.status (six states), setup_state.status (pending, done, skipped), settings.value json_valid, cancel_requested 0/1, attempt >= 1, units >= 0, version >= 1; tier left unconstrained; usage_records.success NULL while a call is in flight; progress INTEGER 0-100; no STRICT tables because Linux Python builds may ship SQLite older than 3.37, so every whole-number column also checks `typeof(...) = 'integer'`; indexes tasks(status) and usage_records(provider, requested_at). Changing a CHECK later needs a table rebuild (see migrations README).
- Migration hashes (2026-10-03): `tests/fixtures/migration_hashes.json` records each committed migration's SHA-256 and a test fails if one is edited or deleted; migration files are `-text` in `.gitattributes` so hashes match on every OS. With the user's approval, `0001` was edited once after commit (before any push or real database) to add the `typeof` checks; from then on, fix forward.

## ADR-028 — Critical coverage registry with due milestones (2026-10-03)

- Context: spec 4.3 lists critical modules that do not exist yet (for example `honesty.py` in M6). The check must work now and must not let a critical module silently escape later (renamed, moved or never created).
- Decision: `scripts/check_critical_coverage.py` holds one registry of critical modules, each with the milestone that introduces it:
  - `app/api/security.py` (M0; was listed as `app/api/middleware.py`, see the update below), `app/tasks/runner.py` (M0), `app/onboarding/detection.py` (M1), `app/domain/budgets.py` (M2), `app/services/usage.py` (M2), `app/modules/fetch/guard.py` (M4), `app/domain/honesty.py` (M6).
  - A module that exists must be measured and reach 95% branch coverage, whatever its milestone.
  - A module that does not exist is reported as "not yet present" while its milestone is still ahead. With `--milestone M<n>` (used by `/milestone-acceptance`), a missing module whose milestone is M<n> or earlier fails.
  - A unit test checks that the registry matches the list in spec 4.3, so the two cannot drift.
- Consequences: moving or adding a critical module means updating spec 4.3 and the registry together.
- Update (M0.19, 2026-10-04): the registry named `app/api/middleware.py`, which never existed (ADR-039 put the middleware in `app/api/security.py`), so the middleware's 95% gate never ran; with the user's approval the registry and spec 4.3 now name `app/api/security.py` (100% branches). CI and `verify.sh full` run `--from-progress`, which reads `Current milestone: M<n>` from `docs/PROGRESS.md` and fails when a module due by M<n-1> is missing, so nobody bumps a milestone by hand and a rename fails once its milestone is past. Known gap: the current milestone's own modules are not required while it is in progress (in M0 nothing is); `/milestone-acceptance` runs the inclusive `--milestone M<n>` check as the backstop. A missing or unreadable `Current milestone` line fails closed.

## ADR-029 — App shell: placeholder pages and htmx version (2026-10-03)

- Decision: sidebar links to areas that do not exist yet open a placeholder page with an empty state, so there are no broken links and keyboard navigation works. htmx 2.0.x (latest patch) is vendored now with its version and license noted; the SSE extension and `sse-connect` are added in M2 with the `/events` endpoint.

## ADR-030 — Composition-root files and the `--setup` flag in M0 (2026-10-03)

- Decision: add `app/config.py` (runtime config resolution) and `app/logging_config.py` (logging setup) beside `app/main.py`, `app/wiring.py` and `app/cli.py` as composition-root files, and `app/domain/redaction.py` as a pure redaction helper. Layers must not import the composition root. `--setup` is parsed and stored in the runtime config in M0 and has no behavior until M1.
- Consequences: spec 2.13 updated.

## ADR-031 — M0 dependency set (2026-10-03)

- Decision: approved by the user.
  - Runtime: `fastapi`, `uvicorn` (without `[standard]`), `jinja2`, `pydantic>=2`, `platformdirs`.
  - Development only: `ruff`, `mypy`, `pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis`, `import-linter`, `pre-commit`, `playwright`, `httpx` (test client), `pyyaml` with `types-PyYAML`.
  - Build backend: `hatchling`.
- Consequences: the remaining libraries from spec 2.12 (keyring, claude-agent-sdk, nh3, icalendar, tomli-w, PDF and Word libraries) are added in the milestone that needs them, each with the user's approval. `httpx` becomes a runtime dependency in M4.

## ADR-032 — CI operating-system policy (2026-10-03)

- Context: the repository is private, so GitHub Actions minutes count against the plan (Windows ×2, macOS ×10).
- Decision: lint and fast tests run on Ubuntu on every push and pull request. macOS and Windows run on pull requests to `main`, on manual dispatch (`workflow_dispatch`) and on a weekly schedule. Before a milestone is accepted, a run on all three operating systems must be green.
- Consequences: AC-M0.7 is met by a manually dispatched or pull-request run on all three systems.

## ADR-033 — How the module boundaries are enforced (2026-10-03)

- Context: spec 2.3 is enforced with import-linter 2.15 (M0.2). The spike found:
  - forbidden contracts accept standard-library modules and packages that are not installed yet;
  - protected contracts fail while nothing imports the package, or while an allowed importer does not exist yet;
  - submodules of external packages (for example `urllib.request`) cannot be forbidden.
- Decision: nine contracts in `pyproject.toml`, each self-tested in `tests/integration/test_import_contracts.py`.
  - Layering, top to bottom: composition root (`app.cli`, `app.main`, `app.wiring`, `app.config`, `app.logging_config`; they may import each other), `app.api`, `app.onboarding | app.tasks`, `app.services`, `app.modules | app.render`, `app.domain`. A `|` means the two sides must not import each other, which is stricter than the spec says about `app.modules` and `app.render`.
  - The API and task-handler contracts check direct imports only, because routes and handlers legitimately reach storage through services. The services contract also checks indirect imports, so a module interface that re-exports a provider still fails.
  - Vendor SDKs (rule 3) are forbidden contracts over all of `app`, exempting the provider package: `app.modules.ai.providers` for `claude_agent_sdk`, `app.modules.secrets.providers` for `keyring`, `app.modules.fetch` for `httpx`. M1 narrows the first two to `claude_sdk` and `keyring_store` once those files exist. Each further vendor library (icalendar, nh3, weasyprint, python-docx, pypdf or pdfplumber) gets its contract in the milestone that installs it.
  - Rule 7: `app.onboarding`, `app.render` and `app.tasks` must not import any `providers` package; services and the API are already blocked by their own contracts.
  - The domain's network submodules (`http.client`, `http.cookiejar`, `http.server`, `urllib.request`) are blocked by an AST test (`tests/unit/test_domain_purity.py`), so pure parts such as `urllib.parse` stay allowed.
  - A test checks every entry of every forbidden contract, and another checks that the expected legal import chain keeps every contract.
- Consequences: because tasks sit above services, services cannot import `app.tasks`; enqueueing from a service goes through an interface injected by `app/wiring.py`. Whether the task runner may call module interfaces directly (spec 2.5 diagram) or only services (spec 2.3 rule 6) is open with the user; the contract currently follows rule 6.

## ADR-034 — The task runner works only through services (2026-10-03)

- Context: spec 2.3 rule 6 says task handlers only call services; the spec 2.5 diagram shows the runner calling the AI module and saving results itself (open question in ADR-033).
- Decision: rule 6 wins. The task runner and its handlers call service functions only; services call the AI module, record usage and save drafts. The "Task handlers stay thin" contract stays as it is.
- Consequences: the spec 2.5 diagram is read as the logical flow; in code, the runner's "AI call" and "save draft + mark succeeded" steps are service calls. Services enqueue tasks through an interface injected by `app/wiring.py`.

## ADR-035 — e2e browser selection and vendored-file handling (2026-10-03)

- Context: the e2e fixtures use Playwright's managed Chromium (`uv run playwright install chromium`). Some environments, such as the cloud sandbox used for development, only have a different pre-installed Chromium build that Playwright won't pick by itself.
- Decision: the test-only variable `JOBPIPELINE_E2E_CHROMIUM` points the e2e fixtures at a Chromium executable; when unset, Playwright's managed Chromium is used. Vendored third-party files (`tests/e2e/vendor/`, `app/static/vendor/`) are marked `-text` in `.gitattributes` so every OS checks them out byte-identical to upstream, which their hash tests rely on.
- Consequences: spec 7.3 lists the variable. Managed Chromium is proven by the CI e2e job (M0.19); M0.4's evidence used a pre-installed Chromium 141 driven by Playwright 1.63.

## ADR-036 — Interface refinements to spec 2.4 (2026-10-03)

- Context: M0.8 wrote the spec 2.4 interfaces as code; spec 2.4 says its shapes hold unless an ADR changes them.
- Decision:
  - Fetching is exposed to services as an injected `JobPageFetcher` protocol (`async fetch(url, limits) -> FetchedPage`) instead of a bare module function, so services receive it through wiring like every other integration (spec 2.3 rule 7) and tests can substitute a fake. The SSRF-guarded `fetch_job_page` from spec 2.4 is implemented in M4 behind this protocol.
  - `AIRequest.output_schema` and `AIResponse.data` are typed `Mapping[str, Any]` (read-only views) rather than `dict`, matching the frozen dataclasses.
  - Fields the spec leaves unspecified are minimal: `ConnectionResult(ok, message, model)`, `ModelChoice(id, label)`, `TokenUsage(input_tokens, output_tokens)`, `CancelToken.cancelled`, `SearchResult(title, url, snippet)`, `EventData(title, starts_at, ends_at, time_zone, details)` with aware and ordered times, `CalendarFile(filename, content, media_type)`, `ExternalRef(provider, external_id)`, `FetchLimits(max_bytes=5 MB, timeout_seconds=15, max_redirects=5)`, `FetchedPage(requested_url, final_url, status, content_type, text)`.
  - Protocol data members (`name`, `capabilities`, `mode`, `storage_allowed`, `reports_usage`, `supports_sync`, `available`) are read-only properties, so implementations may use plain or narrower attributes or computed properties.
  - `EventData` carries a stable `uid` (RFC 5545), so re-exporting an event updates it instead of duplicating it; calendar providers take an injected `Clock` for timestamps.
  - Fetchers clamp `FetchLimits` to the spec 2.9.6 maximums; `max_redirects=5` is an added limit. Unfetchable pages raise `FetchRefused` or `NetworkUnavailable`.
- Consequences: spec 2.4 points here. Later milestones may extend these types (ADR-026), each change with a test.

## ADR-037 — Logging details (2026-10-03)

- Context: M0.10 implements JSON logging with rotating files and a redaction filter.
- Decision: 5 x 1 MiB means 1 MiB per file, the live file plus 4 backups; log folder 0700 and files 0600 on POSIX; redaction covers registered secrets and their repr/JSON/bytes/URL-encoded forms plus case-insensitive `sk-ant-<kind><n>-` token shapes; the filter is on our root handlers only, so `configure_logging` must run before other handlers exist and uvicorn needs `log_config=None` (startup wiring in M0.15); Python warnings are captured into logging; extras never overwrite the core JSON fields and uvicorn's `color_message` is dropped.
- Consequences: spec 2.10 updated.

## ADR-038 — Database and migration runner details (2026-10-03)

- Context: M0.11 implements the SQLite database wrapper and forward-only migration runner. The implementation requires decisions on transaction handling, migration state, backups, and file permissions.
- Decision: code lives in `app/modules/storage/{database.py,runner.py}`, SQL files in `app/modules/storage/migrations/`. One connection with `isolation_level=None` and `check_same_thread=False`; `transaction()` holds an `RLock` so threads take turns (revisit with per-thread connections if contention appears). The runner creates `schema_migrations` itself; "existing database" means at least one recorded migration. Backup via the sqlite3 backup API named `app-<UTC stamp>-before-<first pending NNNN>.db`; the last 5 runner-named backups kept, other files untouched. Misnamed .sql files, duplicate numbers, migrations ending their own transaction, versions in the database unknown to the app, and new migrations numbered below an applied one are all `MigrationError`. A new database file, its WAL/SHM and backups are 0600 and created folders 0700 on POSIX (existing data folders are not chmodded).
- Consequences: spec 2.7 unchanged except an optional one-line note on permissions if fitting. M0.12 and M0.13 acceptance criteria updated to note the runner creates schema_migrations and all database access goes through `Database.transaction()`.

## ADR-039 — Request guard details (2026-10-03)

- Context: spec 2.9 rules 1-3 left details open; code and security reviews found a client-controlled token exemption and a clickjacking gap.
- Decision: `app/api/security.py` is a pure ASGI middleware added last in `create_app` (outermost). Unknown `Host` header → 400. `Host` must be exactly `127.0.0.1:<port>` or `localhost:<port>` (bare host also accepted when port is 80). Token-free requests are only `GET`/`HEAD` without any `HX-Request` header to a route tagged `PAGE_TAG`, plus `GET`/`HEAD` `/health` and `/static/` (everything else, including unknown paths and `/api`, needs the token). Token compared with `hmac.compare_digest`. State-changing requests need `Origin` equal to `http://<Host actually used>`, or when `Origin` is absent a `Referer` with that origin and no userinfo. Null `Origin` refused. Any repeated `Host`, `Origin` or token header refused. WebSockets refused. FastAPI's generated `/docs`, `/redoc` and `/openapi.json` disabled. Every response carries `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin` (approved by the user 2026-10-03).
- Consequences: spec 2.9 rules 11-13 added; page routes must be registered with `tags=[PAGE_TAG]`; `GET` routes must be side-effect free; uvicorn started with `host="127.0.0.1"`, `proxy_headers=False`, `log_config=None` and no root path (M0.15); M2 decides how `/events` (SSE stream) is authorised without the token in the URL.
- Update (M0.16, 2026-10-04): the token exemption is decided from the route dispatch will actually use — the first route in `app.router.routes` that fully matches; only a page-tagged APIRoute counts, so an earlier non-page route on the same path still needs the token. FastAPI 0.142 wraps included routers in a private `_IncludedRouter` the middleware can't see into, so pages are registered directly on the app with `app.api.pages.register_pages(app)`; a PAGE_TAG route in an included router needs the token (fails closed). The middleware sets `Cache-Control: no-store` on every page response. A test walks included routers (private API, on purpose) to fail loudly if FastAPI changes.

## ADR-040 — Command start-up and exit codes (2026-10-03)

- Context: M0.15 implements the CLI command with flags, 127.0.0.1 binding, browser opening and proper start-up error handling.
- Decision: app/cli.py start-up order is resolve config -> create the data folder (mode 0700 when the CLI creates it; parent folders and an existing folder keep their permissions, as ADR-038) -> refuse a data folder owned by another user (ADR-043) -> configure_logging -> exception hooks -> create_app -> register the launch token with the redactor -> port pre-check by binding 127.0.0.1 (SO_REUSEADDR on POSIX only; never on Windows, where it would allow port hijacking) -> browser poller -> uvicorn with host 127.0.0.1, proxy_headers False, log_config None, root_path "". Exit codes: 0 normal stop, 1 port in use or start-up failure (including uvicorn's own sys.exit on lifespan or bind failure, which the CLI maps to 1), 2 invalid setting or option, 130 Ctrl+C before the server starts; SIGTERM ends the process with 143 (uvicorn re-raises the signal). Errors print one plain "jobpipeline: ..." line; the console formatter never prints tracebacks and cuts message text where a traceback begins (uvicorn logs Starlette's traceback as text); the log file keeps the details. The browser opens once after /health returns 200 (polled every 0.25 s for about 10 s, through an opener that ignores proxy settings and refuses redirects); if it never answers, a warning tells the user the URL to open.
- Consequences: the M0.16 smoke test must not rely on the console script on macOS (see below).
- Update (M0.16, 2026-10-04): once serving has begun, Ctrl+C is a normal stop with exit 0 (uvicorn re-raises it after a clean shutdown); 130 only before serving begins. On Windows the CLI maps SIGBREAK to KeyboardInterrupt while serving, because uvicorn re-raises Ctrl+Break and Python's default for it exits with code 3 and skips clean-up. `python -m app` is equivalent to the `jobpipeline` command. Windows behaviour is unverified until the Windows CI run (M0.19).
- Update (M0.19, 2026-10-04): Windows CI run https://github.com/goncalodasilva/job-pipeline/actions/runs/37194424406 verified smoke tests stop with exit 0 on both Python 3.11 and 3.14, confirming Ctrl+Break handling.

## ADR-041 — Hardened htmx configuration and shell error messages (2026-10-04)

- Context: security and code review of M0.17. htmx 2.0.11 defaults allow eval (`allowEval: true`), swapped-in script tags (`allowScriptTags: true`), a localStorage/sessionStorage history cache (`historyCacheSize: unlimited`), and history restores sent as HX-Request (which would 403 without the token and return a fragment).
- Decision: `app/templates/base.html` pins `<meta name="htmx-config">` before the htmx script with `selfRequestsOnly: true, allowEval: false, allowScriptTags: false, historyCacheSize: 0, refreshOnHistoryMiss: true, historyRestoreAsHxRequest: false`. Consequence: `hx-on*` attributes, `js:` values in triggers, and trigger filters are unavailable; reactive behaviour lives in `app/static/js/app.js` using `addEventListener`. `app/render.py` `render()` function returns the full page when `HX-History-Restore-Request: true` (not a fragment). App vendored files and their metadata live in `app/static/vendor/README.md`, recording htmx version, npm tarball sha512 integrity hash, extracted SHA-256, and the 0BSD license. `app.js` shows template-provided messages (via `data-msg-*` attributes on `#app-status`, per spec 3.10) both visibly and in the `#announcer` live region: a 403 response shows "restarted" with a Reload button; `htmx:sendError` shows "offline"; other errors show "failed". Fragments mark their focus target with `data-autofocus="true"`.
- Follow-ups (not done): (a) the `/static/` token exemption is currently decided by a path-prefix check in the middleware; a test asserts `/static/` dispatches to the "static" Mount first (correct), but when pattern routes arrive, the middleware should detect exemptions from the first-matching route like pages do (security reviewer, low priority); (b) every 403 currently shows the restart message; once other 403 scenarios exist, key the message off a middleware marker header; (c) when `hx-boost` and `hx-push-url` arrive in M1 or later, fragment navigation must update the page `<title>` and the active link's `aria-current`; (d) empty-state copy for each area lives in `app/api/pages.py` under `AREAS` until each area receives its own template.

## ADR-042 — App shell styling and responsive sidebar (2026-10-04)

- Context: the app shell (M0.17) needs styles for light and dark themes, a responsive sidebar that collapses on narrow screens, and accessibility features (focus visibility, target size, motion respect).
- Decision: one hand-written stylesheet `app/static/css/app.css`, no build step. Colour tokens on `:root` (`--color-bg`, `--color-surface`, `--color-text`, `--color-text-muted`, `--color-border`, `--color-accent`, `--color-accent-soft`, `--color-focus`, `--color-warning-bg`, `--color-warning-border`); dark values follow `prefers-color-scheme`, with a `data-theme="light|dark"` override on `<html>` ready for the later Appearance setting (dark palette written twice; keep in sync). All text/link/focus pairs meet WCAG 2.2 AA in both schemes. Breakpoint 768 px: below it the sidebar collapses behind a top-bar "Menu" button (`aria-controls`/`aria-expanded`) and is pushed into the page flow rather than overlaid (no focus trap needed); opening focuses the first link; Escape closes and refocuses the toggle only when focus is in the menu or on the toggle and no other handler used the key. The collapse applies only when an inline head script sets `html.js`, so without scripts phone users still get the plain links. Focus: 3px `:focus-visible` outline. Targets 44 px. `prefers-reduced-motion` reduces all transitions/animations to 0.01ms. `forced-colors` rule keeps the current-area marker visible.
- Deviations and follow-ups: the design's "collapsible left sidebar" on desktop is not built (only the narrow-layout collapse) — revisit with the Appearance/density settings (M9 or when settings land); desktop sidebar is not sticky on long pages; density toggle (comfortable/compact) not built; WCAG reflow at 320 px is untested (tests use 360 per spec 3.9); status banner might gain a visible lead word or icon once other status kinds share it; if sidebar links later use htmx navigation, close the sidebar and move focus after the swap.

## ADR-043 — Refuse a data folder owned by another user (2026-10-04)

- Context: security review of M0.19. The CLI used an existing data folder without checking it. The database, backups and logs are 0600, but whoever owns the folder can delete or replace files in it or plant symlinks (for example `app.db` pointing elsewhere), and later files such as CV exports would land there. Only realistic with `--data-dir` in a shared place such as `/tmp` or a folder another account created; the default folder sits inside the user's home.
- Decision (option A, chosen by the user): on POSIX, after creating the folder (mode 0700) and before anything is written into it, the CLI exits with code 2 and one plain line ("The data folder … belongs to another user. Choose a folder you own with --data-dir.") when the folder's owner differs from the effective user id. Checking after `mkdir` also catches a folder someone created between the two steps. `stat` follows symlinks, so the folder a link points to is what counts. Existing folders the user owns keep their permissions (ADR-038 unchanged). Running as root against a user's folder is refused too, which keeps root-owned files out of it.
- Not covered: parent folders (a writable parent could swap the data folder after the check; not a realistic threat under the home folder) and Windows, which has no POSIX owner ids and is documented, not checked.
- Consequences: README troubleshooting explains the message and warns against `sudo`. When M1 starts writing the settings file, the config folder gets the same rule.

## ADR-044 — M1 plan decisions (2026-10-04)

- Context: `/plan-milestone M1` raised fourteen questions where the spec, the detailed design and the M0 code disagree or leave a gap. The user accepted the planner's recommendations ("go with your recommendations"). Later tasks record their own findings in new ADRs (the M1.1 Agent SDK spike findings in its own ADR).
- Decision:
  1. Dependencies: approved in principle for M1: `claude-agent-sdk` and `keyring` (runtime, spec 2.12, ADR-007), `jsonschema` (runtime, Draft 2020-12 validation), and `pyyaml` promoted from dev to runtime for prompt front matter (`safe_load` only). No `tomli-w`: `config.toml` is written by hand and round-trip tested (M1.4). Each package is added in the task that needs it, with its exact version recorded in that task's ADR and its vendor import-linter contract (ADR-033).
  2. App-selectable fakes (closes ADR-026's open item): `FakeAIProvider` lives in `app/modules/ai/providers/fake.py` and `MemorySecretStore` in `app/modules/secrets/providers/memory.py`. Wiring offers the fake, labelled "Fake (testing only)", only when `JOBPIPELINE_ALLOW_FAKE_PROVIDERS=1`; its outcome is chosen by fictional keywords in the pasted key. Per-test scripting helpers stay in `tests/fakes/`. The e2e subprocess uses the real `KeyringSecretStore` with a test keyring backend set through `PYTHON_KEYRING_BACKEND`, and an autouse fixture pins an in-memory backend for every test, so tests never reach the real keychain. Spec 4.2 is updated to match.
  3. Deferred workspace: on a first run the app starts without creating the default data folder or database; step 1 creates them. Until then logs are buffered in memory and `GET /health` returns 200 with `database: "not_created"` (amends ADR-023, which returned 503 without a database).
  4. Step 1 with `--data-dir` or `JOBPIPELINE_DATA_DIR` and no bootstrap config is still a first run: the folder field is read-only and step 1 writes `config.toml`. Demo mode never writes `config.toml` and runs the guide inside the demo folder. "Try the demo" shows the `jobpipeline --demo` command.
  5. `--setup` reopens the guide at its first step: page loads redirect to the guide until "Back to the app" (a POST); every step stays editable with its saved values; nothing is reset.
  6. In M1 the connection test is awaited inside its POST request with a timeout, because the task runner arrives in M2. From M2 it reserves a usage record and may become a task.
  7. Secrets: the keychain wins over environment variables; environment variables are read-only; keyring service name `jobpipeline` (`jobpipeline-demo` in demo mode, so demo runs never touch real credentials); saved keys are shown with no characters; in subscription-token mode the provider blanks `ANTHROPIC_API_KEY` for the SDK call.
  8. Step 5 requires at least one target role and the CV country; everything else is optional. Countries come from a static ISO 3166-1 JSON file shipped with the app.
  9. Models: `known_models` uses aliases, Fast → Haiku and Thorough → Sonnet; records store the model the provider reports. Confirmed or revised after the M1.1 spike.
  10. Live tests read credentials only from `JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN` and `JOBPIPELINE_LIVE_ANTHROPIC_API_KEY`, never from the keychain. The API-key live test skips when its variable is absent (approved skip under golden rule 6).
  11. Typed settings models (pydantic) may live in `app/domain/settings.py`: pydantic is the validation library from spec 2.12, not a web framework, and does no I/O.
  12. The `ProviderNotInstalled` copy (spec 2.11) and README prerequisites are reworded after the M1.1 spike if the SDK bundles the Claude Code CLI; mapped messages may add one mode-specific line (for example "run `claude setup-token` again").
  13. The prompt loader takes the default prompts folder from wiring; packaging `prompts/` into the wheel is settled in M9.
  14. Still open: the Anthropic terms URL linked from the provider step. Ask the user during M1.17.
- Consequences: spec 2.13, 4.2 and 7.3 and ADR-023 are updated in the tasks that implement these points (M1.5, M1.10, M1.25). Known gap: M1 has no optional setup step, so AC-M1.7's "Done lists skipped steps" is proven with a test-registered optional step until steps 3 and 4 arrive (M2, M3). Provider health for case 4 is computed at start-up and after POSTs and cached, so GET routes never read secrets (spec 2.9.12). Docs nit: ADR-041's `app/render.py render()` is `app/api/templating.render`.
