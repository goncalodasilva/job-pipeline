---
name: htmx-ui
description: "Conventions for server-rendered pages and htmx fragments: routes, templates, launch token, focus management and screen-reader announcements after swaps, live-update events read by a fetch reader (no EventSource), modal dialogs and blocked actions, drag-and-drop with a keyboard alternative, shortcuts, empty/loading/error states and phone layouts. Use whenever work touches app/templates/, app/static/ or page and fragment routes."
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
- It dispatches two bubbling CustomEvents on `document.body`, both with no detail: `tasks:resync` on every connection (and when a tab joins or is shown again) and `tasks:updated` for each task change. Only these two come from the stream. The server also sends `tasks:resync` in an `HX-Trigger` header after a cancel or retry (`app/api/tasks.py`). Events can arrive out of order, so a listener always refetches; never read a status from the event.
- Parts that show tasks refresh with a trigger from the body, for example `hx-trigger="tasks:resync from:body, tasks:updated from:body"` (htmx 2.0 `from:` modifier). No trigger filters, `js:` values or `hx-on*` attributes (ADR-041).
- A refreshed fragment is an ordinary GET fragment route: it needs the token and has no side effects.
- The reader reconnects by itself (backoff 1 to 30 s), pauses while the tab is hidden, shows the restart message on a 403, and shares one stream per browser between tabs (ADR-077, M2.18 update). Don't open another stream or an `EventSource`.
- Two more body events come from HTTP answers, not from the stream. `usage:changed` is sent in an `HX-Trigger-After-Settle` header by a budget save or removal, once the swap has settled; the usage panel and Testing tools refetch on it, never on `tasks:resync`. `tasks:finished` is sent in an `HX-Trigger` header by every `GET /tasks/indicator` answer, with the detail `{"tasks": [{"key", "message", "age"}]}`; app.js announces each one once and keeps its key in localStorage. Full list and refresh rules: spec 2.12 and ADR-088.

## Dialogs, blocked actions and saves

- **Modal question** (ADR-086): a native `<dialog data-modal role="dialog" aria-modal="true" aria-labelledby aria-describedby>` in a fragment, for a question that must be answered before an action starts (for example the budget warning). app.js calls `showModal()` after the swap settles, so the page behind it is inert. Initial focus goes to the safe choice (`autofocus` on "Not now"). Esc and "Not now" close it and send nothing. Tab wraps while it is open. `data-return-focus="<selector>"` names the control that gets focus back when it closes without a start. The confirmed start posts the action's own values plus the confirmation fields.
- **Never refresh a region that holds an open dialog.** A request whose own element holds an open dialog is cancelled (`htmx:beforeRequest` in app.js). A page that can change budgets refreshes its blocked actions on `usage:changed`; Testing tools refetches itself on `usage:changed`.
- **Blocked action** (ADR-086): a focusable button with `aria-disabled="true"`, not the `disabled` attribute, so keyboard users reach it and hear the reason. `aria-describedby` names the reason above it, which gives the reset date and a link to Settings. The server refuses the same start with 409, so the client guard is a convenience only.
- **Saves** (ADR-081, ADR-087): a successful save answers with a line in its block ("Saved.", or "Removed. This service has no limit now.") and moves focus to the block's heading (`tabindex="-1" data-autofocus`). The same words go to the live region through `HX-Trigger: {"announce": "..."}`. Never use `HX-Push-Url` for a settings save.
- **Refused and stale answers**: a refused form answers 422, with each error tied to its field (`aria-describedby`, `aria-invalid="true"`) and the first invalid field focused. A stale version answers 409 with the reload prompt, `fragments/shell/_reload_prompt.html` (`role="alert"`, the form's own explanation and a "Reload the page" link). Any form that saves to a versioned setting uses the prompt, and the 409 stays inside the form's block (ADR-081).

## Layout and accessibility

- Mobile first: layouts work from 360 px; the sidebar collapses; tables scroll inside their own container.
- A scroll container that the page can't otherwise reach is a region: `role="region" tabindex="0" aria-labelledby="<caption id>"` (see `fragments/settings/_usage_panel.html`). The e2e tests check that keyboard users can reach and scroll it, and that the page itself doesn't scroll sideways at 360 px.
- Status uses text or an icon as well as color; contrast meets WCAG 2.2 AA; targets are at least 24 by 24 px; animations respect reduced motion.
- Forms have visible labels; errors are tied to fields with `aria-describedby`.

## Tests for UI work

- Integration test: the route returns the fragment for htmx requests and the full page otherwise; token and origin rules hold.
- E2E (Playwright): the scenario at desktop and 390x844; a keyboard-only path; focus lands where intended after swaps; axe shows no serious or critical violations.
- Ask `ux-reviewer` to compare the result with `docs/references/detailed-design.md`.
