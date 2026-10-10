---
name: htmx-ui
description: "Conventions for server-rendered pages and htmx fragments: routes, templates, launch token, focus management and screen-reader announcements after swaps, server-sent events, drag-and-drop with a keyboard alternative, shortcuts, empty/loading/error states and phone layouts. Use whenever work touches app/templates/, app/static/ or page and fragment routes."
---

# Pages and fragments with htmx

Read `references/patterns.md` for code. These rules apply to every UI change.

## Routes and templates

- One route module per area in `app/api/`. A route returns a full page for normal requests and only the fragment when `HX-Request: true` (use the shared `render()` helper).
- Templates: `app/templates/pages/<area>.html` extend `base.html`; fragments live in `app/templates/fragments/<area>/_<name>.html` and never extend anything.
- Routes call services only. No SQL, no providers, no business rules in routes or templates.
- Every non-page request carries the launch token through `hx-headers` on `<body>`; never remove it or add a route that skips the token middleware.
- Page routes are registered directly on the app through `register_pages` (app/api/pages.py) with `tags=[PAGE_TAG]`, never through an included router; every other route needs the token. GET routes never have side effects (spec 2.9 rule 12).

## Interaction rules

- After every swap that changes what the user is working on, move focus deliberately (the swapped region's heading or first control) and announce the result in the global `aria-live` region.
- Every drag-and-drop has a keyboard alternative (for the board, a status menu on each card) that calls the same endpoint.
- Shortcuts are registered in `app/static/js/shortcuts.js`, listed in the `?` help, ignored while typing in inputs, and can be switched off in settings.
- Reversible actions happen at once and offer undo; confirmations are only for irreversible actions.
- Long work goes through the task runner; the button swaps to a task status fragment that refreshes on `tasks:resync` (see Live updates).
- Every list and panel has its empty, loading and error states, with the copy tone from the detailed design.

## Live updates

- Pages read `GET /events` with the small fetch-based reader in `app/static/js/app.js`. There is no `EventSource` and no htmx SSE extension (ADR-077).
- A page opts in with `{% set live_updates = true %}` before `{% extends %}`-level content renders; `base.html` then puts `data-events-url="/events"` on `<body>`. Opt in exactly on pages that include the tasks slot (every app page); guide pages never do, so they open no stream.
- The reader sends `X-JP-Token` as a header, taken from `<body hx-headers>` on every attempt. The token is never in a URL.
- It dispatches two bubbling CustomEvents on `document.body`, both with no detail: `tasks:resync` on every connection (and when a tab joins or is shown again) and `tasks:updated` for each task change. Events can arrive out of order, so a listener always refetches; never read a status from the event.
- Parts that show tasks refresh with a trigger from the body, for example `hx-trigger="tasks:resync from:body, tasks:updated from:body"` (htmx 2.0 `from:` modifier). No trigger filters, `js:` values or `hx-on*` attributes (ADR-041).
- A refreshed fragment is an ordinary GET fragment route: it needs the token and has no side effects.
- The reader reconnects by itself (backoff 1 to 30 s), pauses while the tab is hidden, shows the restart message on a 403, and shares one stream per browser between tabs (ADR-077, M2.18 update). Don't open another stream or an `EventSource`.

## Layout and accessibility

- Mobile first: layouts work from 360 px; the sidebar collapses; tables scroll inside their own container.
- Status uses text or an icon as well as color; contrast meets WCAG 2.2 AA; targets are at least 24 by 24 px; animations respect reduced motion.
- Forms have visible labels; errors are tied to fields with `aria-describedby`.

## Tests for UI work

- Integration test: the route returns the fragment for htmx requests and the full page otherwise; token and origin rules hold.
- E2E (Playwright): the scenario at desktop and 390x844; a keyboard-only path; focus lands where intended after swaps; axe shows no serious or critical violations.
- Ask `ux-reviewer` to compare the result with `docs/references/detailed-design.md`.
