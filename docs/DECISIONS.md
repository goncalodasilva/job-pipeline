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

## ADR-028 — Critical coverage registry with due milestones (2026-10-03)

- Context: spec 4.3 lists critical modules that do not exist yet (for example `honesty.py` in M6). The check must work now and must not let a critical module silently escape later (renamed, moved or never created).
- Decision: `scripts/check_critical_coverage.py` holds one registry of critical modules, each with the milestone that introduces it:
  - `app/api/middleware.py` (M0), `app/tasks/runner.py` (M0), `app/onboarding/detection.py` (M1), `app/domain/budgets.py` (M2), `app/services/usage.py` (M2), `app/modules/fetch/guard.py` (M4), `app/domain/honesty.py` (M6).
  - A module that exists must be measured and reach 95% branch coverage, whatever its milestone.
  - A module that does not exist is reported as "not yet present" while its milestone is still ahead. With `--milestone M<n>` (used by `/milestone-acceptance` and CI), a missing module whose milestone is M<n> or earlier fails.
  - A unit test checks that the registry matches the list in spec 4.3, so the two cannot drift.
- Consequences: moving or adding a critical module means updating spec 4.3 and the registry together.

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
