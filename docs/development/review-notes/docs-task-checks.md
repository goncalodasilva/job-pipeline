---
name: docs-task-checks
description: What goes wrong in documentation tasks (README, spec alignment, skills, acceptance drafts) and how to check them; read when reviewing a docs-only change
metadata:
  type: project
---

Moved from MEMORY.md on 2026-10-11 to keep the index short. Verify names against the code before citing them.

- Docs tasks (README/spec layout/env tables) routinely state plausible but wrong facts (file locations, CI scope per OS, where a setting is edited, env precedence, demo contents): grep each claim against code/workflows; flag credential placeholders inline in shell commands (history) and docs that contradict each other on that.
- Docs-alignment tasks (spec/skills/acceptance draft, M2.27): facts are mostly right; the gaps are (1) skills rewritten from the one worked example copy its special cases (fixed `fake` service, section-not-task-fragment answer) and omit what a real type needs (service from the saved provider kind for gate AND meter, AI task in `TASK_SPECS`, blocking calls off the loop, fake-output labelling); (2) event lists that say "no detail" for an event with a payload (`tasks:finished`); (3) README names ("practice provider") that differ from the UI labels ("Fake (testing only)", "Fake provider"); (4) acceptance drafts that gather open items from the last tasks only, leaving earlier unconfirmed test changes out.
