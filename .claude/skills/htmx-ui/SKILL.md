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
- Page routes are registered with `tags=[PAGE_TAG]` (app/api/security.py); every other route needs the token. GET routes never have side effects (spec 2.9 rule 12).

## Interaction rules

- After every swap that changes what the user is working on, move focus deliberately (the swapped region's heading or first control) and announce the result in the global `aria-live` region.
- Every drag-and-drop has a keyboard alternative (for the board, a status menu on each card) that calls the same endpoint.
- Shortcuts are registered in `app/static/js/shortcuts.js`, listed in the `?` help, ignored while typing in inputs, and can be switched off in settings.
- Reversible actions happen at once and offer undo; confirmations are only for irreversible actions.
- Long work goes through the task runner; the button swaps to a task status fragment, and the SSE `task-updated` event refreshes it.
- Every list and panel has its empty, loading and error states, with the copy tone from the detailed design.

## Layout and accessibility

- Mobile first: layouts work from 360 px; the sidebar collapses; tables scroll inside their own container.
- Status uses text or an icon as well as color; contrast meets WCAG 2.2 AA; targets are at least 24 by 24 px; animations respect reduced motion.
- Forms have visible labels; errors are tied to fields with `aria-describedby`.

## Tests for UI work

- Integration test: the route returns the fragment for htmx requests and the full page otherwise; token and origin rules hold.
- E2E (Playwright): the scenario at desktop and 390x844; a keyboard-only path; focus lands where intended after swaps; axe shows no serious or critical violations.
- Ask `ux-reviewer` to compare the result with `docs/references/detailed-design.md`.
