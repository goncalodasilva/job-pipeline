---
paths:
  - "app/templates/**"
  - "app/static/**"
  - "app/api/**"
---

# UI rules

- Follow the `htmx-ui` skill and match `docs/references/detailed-design.md`.
- Pages extend `base.html`; fragments live under `fragments/` and never extend.
- Keep the launch token on every non-page request (`hx-headers` on `<body>`).
- After swaps: deliberate focus (`data-autofocus`) and an announcement in the live region.
- Every drag-and-drop has a keyboard alternative; every list has empty, loading and error states.
- Layouts work from 360 px; status never relies on color alone.
- No `|safe` on AI or user content.
