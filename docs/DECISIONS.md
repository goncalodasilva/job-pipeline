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
