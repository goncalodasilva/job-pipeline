# One-shot prompt: finish Job Search Pipeline v1.0 in one push

Paste everything below the line into the agent (Codex, Claude Code or similar), started at the root of a clone of this repository on branch `m9-polish-v1`.

Version 2 (2026-11-07): a first Codex run of version 1 ran out of credits partway through M9.5. Its unfinished work is committed as a WIP commit, and section 0 below describes where it stopped.

---

## 0. Start here: new machine, unfinished run

**Set up the machine (once):**
1. `git checkout m9-polish-v1 && git log --oneline -3`. The top commit is `wip(m9): … handoff` and holds the previous run's unfinished M9.5 work. Build on top of it. Don't amend, squash or rebase it: history is never rewritten here.
2. `uv sync`, `uv run pre-commit install`, `uv run playwright install chromium`.
3. `bash scripts/verify.sh quick` once to get a baseline. Expect the failures listed below. Anything else is a difference between machines: note it and fix it.
4. Logs that earlier session entries mention under `/private/tmp/...` were on the other machine and are **not** available. Don't look for them. Write your own logs to a temp folder.
5. On macOS, wrap long runs in `caffeinate -dims`. On Linux, skip that.

**Where the previous run stopped (what's in the WIP commit):** see the last session log entry in `docs/PROGRESS.md`, headed "handoff for the one-shot finish". In short:

- **M9.5 part A (palette):** implemented with 44 tests (`tests/palette_pages.py` is the contract). Its own tests passed. Not reviewed. The 600 px top-bar wrap fix may or may not be in `app/static/css/app.css`.
- **Flakes:** `tests/e2e/test_swap_binding.py` is a new failing-first test for TD-320 (a form clicked before htmx settles must still send the token). Red, app fix not done. `tests/integration/test_m7_cross_cutting.py` was edited for TD-321. Its last focused run still failed `test_session_drafts_never_use_never_use_facts`.
- **M9.5 part B (shortcuts):** drafted in `app/static/js/shortcuts.js`, `app/static/js/app.js`, `fragments/shell/_shortcuts.html` and `_shortcut_list.html`. In `tests/e2e/test_global_shortcuts.py` and related tests, 10 passed and 1 failed (`test_single_key_shortcuts_off_keeps_ctrl_k`).
- **M9.5 part C (demo):** `app/demo_data/workspace.json` (built by `tests/demo_builder.py`), `app/services/demo.py` (seeded from `prepare()` in `app/main.py`), and `--demo` no longer reads the real bootstrap file (`app/config.py`, `app/main.py`). The demo seed tests (43) and the demo journey (18) passed in focused runs.
- **Known breakage at the handoff:**
  1. `uv run lint-imports` fails: `app.services.demo` imports `app.config` (services may not). Pass plain values (data folder, an `is_demo` flag) from `app/main.py` instead.
  2. The last `verify.sh quick` was cut off at 20%. It had already shown about 11 failures in the fast suite (around 10% and 16% of the run). Find them with your baseline run.
  3. The full e2e suite has not run since the palette was added.
- **No reviews ran** on any M9.5 code. Nothing in M9.5 is ticked, and no ADR-138 exists yet.

Treat everything in the WIP commit as **unreviewed draft code**. Keep what's good, fix what's broken, and review it as part of M9.5 like code you wrote yourself.

---

You are finishing a nearly complete application: **Job Search Pipeline**, a local, open-source FastAPI + Jinja2/htmx + SQLite web app for an honest job search. Milestones M0–M8 are done and accepted. In M9 (Polish and v1.0), tasks M9.1–M9.4 are committed. **Only M9.5 and M9.6 remain.** Your job is to take the project from where it is now to a v1.0 release candidate in this one session, without stopping between tasks.

You are a capable model and you are trusted to work that way. Until now the project ran a slow, step-by-step workflow with checkpoints, sub-commits and approval stops after each part, built so smaller models could work safely. **For this run the user is replacing that ceremony with you.** Build first, verify efficiently, review once per task, fix, commit, keep going. When something is ambiguous within the spec, decide, write it down, and move on. Do not stop to ask unless you hit one of the hard stops below.

## 1. What "done" means

All of these are true at the end:

1. M9.5 is implemented, its tests from `docs/PROGRESS.md` exist and pass, and it is ticked `[x]` with an Evidence line.
2. M9.6 parts A–D are implemented, its tests exist and pass, and it is ticked `[x]` with an Evidence line.
3. `bash scripts/verify.sh full` prints `VERIFY full: PASS` on macOS on the final tree. Linux full verification in a local Docker container (ADR-131 item 9) has also run and passed, or it is recorded as left for the user if Docker isn't available.
4. `scripts/check_wheel.py` passes, version is `1.0.0`, and `prompts/` and `app/demo_data/` ship in the wheel.
5. README and `docs/user-guide.md` are written for a newcomer. `docs/acceptance/M9.md` exists with the manual checklist and the M5–M8 carry-over, all **unchecked**.
6. `docs/PROGRESS.md`, `docs/DECISIONS.md`, `docs/TECH_DEBT.md` and the spec sections M9.6 names are updated.
7. There are a few clean local commits on branch `m9-polish-v1`, on top of the WIP handoff commit (see section 6). Nothing is merged or tagged. Push only if the user tells you to.

M10 (automated sourcing) is **out of scope**. It comes after v1.0.

## 2. Where everything is (read in this order, and read smart)

The docs are large (spec ~1,500 lines, decisions ~1,600). **Don't read them end to end.** Use `rg` and `sed -n` to read only the sections you need.

**Ground rules (read in full, they're short):**
- `AGENTS.md`: project guide and golden rules. Section 3 below overrides only its workflow ceremony, not its golden rules.
- `.claude/rules/architecture.md`, `.claude/rules/security-and-privacy.md`: always apply.
- `.claude/rules/ui-htmx.md`, `.claude/rules/tests.md`, `.claude/rules/database.md`: apply to most of this work.

**The task definitions (the most important source):**
- `docs/PROGRESS.md`, section `## M9 — Polish and version 1.0` (around line 627): the full AC, scope, parts and **exact test names** for M9.5 and M9.6. Treat those test lists as your test plan.
- `docs/PROGRESS.md`, the **last two session log entries** (bottom of the file): the detailed handoff of M9.5 part A, then the handoff from the one-shot run (parts B and C drafted, the flake fixes started, known failures). Read both before touching code. Where they disagree, the later one wins.
- `python3 scripts/next_task.py --open`: prints both open tasks in full.

**Spec and design (look up only what you need):**
- `docs/TECHNICAL_SPEC.md`: section 2 architecture (2.4, 2.7, 2.9 security rules, 2.10, 2.11, 2.13), section 3 rules (3.1 honesty, 3.3 untrusted content, 3.6, 3.7, 3.8 accessibility, 3.9 performance), section 4 testing, section 5 → M9 test plan and "As built".
- `docs/references/detailed-design.md`: the UX source of truth. For M9.5 search for "Cross-cutting", "shortcut", "palette" and "demo".
- `docs/DECISIONS.md`: ADR-025, ADR-049 (demo workspace safety), ADR-088, ADR-098 item 7, ADR-131 (the M9 plan, every item), ADR-133 (you may correct tests that are wrong against the spec, as long as you record it and keep coverage), ADR-061 (Windows suspended: **no Windows work**). The last ADR is ADR-137. New ones start at ADR-138.
- `docs/TECH_DEBT.md`: TD-320 and TD-321 (e2e flakes, fix these **first**), plus TD-001, TD-010, TD-011, TD-012, TD-033, TD-111, TD-115, TD-118, TD-165, TD-234, TD-264, TD-300, TD-308.

**How things are done here (procedures and existing code patterns):**
- `.agents/skills/htmx-ui/SKILL.md` (pages, fragments, launch token, focus, announcements, shortcuts, dialogs, phone layout) and `.agents/skills/onboarding-step/SKILL.md` (for the `--demo` startup path). `.agents/skills/verify/` describes verification.
- `docs/development/review-notes/MEMORY.md` plus the files next to it: recurring review findings. Read them **before** writing code so reviewers don't catch the same mistakes again.
- `tests/palette_pages.py`: its docstring is the contract for palette paths, markup, ids and data attributes.
- `app/services/data_portability.py` and `app/modules/storage/portability.py` (M9.1): the demo seed must go **through the importer** (`DataPortabilityService.restore`).
- Look for `is_demo_workspace` and how `--demo` is wired in `app/main.py` / `app/wiring.py` / the CLI.
- `tests/fakes/`: fakes for every provider. Tests never use the network.

## 3. How to work (this replaces the per-task workflow)

**Ignore** the `$plan-milestone` / `$next-task` / `$run-milestone` ceremony for this run: no stopping at checkpoints, no "after-checkpoint" stops, no per-part sub-commits, no waiting for approval between parts. The user approves this deviation for this run. The "Checkpoint: after" items become things you list for the user at the end.

**Decide and record.** When the spec, design and ADRs don't settle something (an open "choice to confirm", a UX detail, a threshold), pick the option most consistent with the spec and the existing code. Write it in **one consolidated ADR-138, "Decisions taken during the v1.0 push"**, as numbered items, and keep going. The open choices from the M9.5 handoff (Add-job and New-event targets, palette not opening over another dialog, query truncation and the 200-status test, company-name matching, untested draft/removed results) belong there. Use reasonable defaults: tighten the overlong-query test to 200, and add tests for draft and removed items.

**Hard stops (the only reasons to stop and ask):**
- Adding a **new runtime dependency** or changing the stack. Also don't add dev dependencies such as `pytest-xdist`. Work with what's installed.
- Anything that **weakens or bypasses the honesty checker** (spec 3.1), or loosens security rules (launch token, host/origin checks, 127.0.0.1 binding, secret handling).
- Needing real credentials, the user's real data folder or config folder, or the real keychain.
- Pushing, merging, tagging, rewriting history, or deleting the user's data.
- The same failure surviving three different real fix attempts. Record it as tech debt and move on to the rest. Only stop if it blocks the whole release.

Everything else, including judgement calls the old workflow would have parked, is yours to make.

**Golden rules still apply, all of them:** honesty with fact ids, no secrets anywhere, fictional data only (`*.example.test` addresses), no network in tests, module boundaries (`uv run lint-imports` must pass, never weaken a contract), untrusted text stays delimited data, and Clock/IdFactory injection (no `datetime.now()` or `uuid4()` in services).

## 4. Testing: still required, but run it efficiently

The full suite is large: about 11,000 fast tests and about 700 Playwright e2e tests, and a full run takes a long time. Don't run it after every change.

- **Write tests with the code, not long before it.** Use the test names from PROGRESS.md for M9.5 and M9.6. Quickly confirm each new test fails for the right reason (one targeted run), then implement. No ceremony beyond that.
- **Inner loop:** run only what you touched, e.g. `uv run pytest tests/integration/test_palette_routes.py -q`, `-k <name>`, `--lf`, plus `uv run ruff check <files>` and `uv run mypy` when types change. For UI work, run only the affected e2e files.
- **Phase gates:** run `bash scripts/verify.sh quick` once each phase is done (format, lint, types, boundaries, fast tests, smoke).
- **Full gates:** run `bash scripts/verify.sh full` **once before each task commit** (two or three times in the whole run), not more. Run it in the background with a log, wrapped in `caffeinate` because this Mac sleeps and stalls long runs:
  `caffeinate -dims bash scripts/verify.sh full > /tmp/v1-full-<n>.log 2>&1`
  Keep working on non-conflicting things (docs, the next part's design) while it runs, and avoid editing files the run depends on.
- **Never use `-x` on a full e2e run.** Collect every failure in one pass, fix them in a batch, then rerun only the failed tests (`--lf`) before the next full gate.
- **Fix the flakes first** (TD-320 native-submit after an htmx swap, TD-321 random id containing "950"). They waste full runs. That's M9.6 part A's first item, pulled to the very start.
- When a pre-existing test breaks because of a deliberate, spec-backed change (like the new top-bar Search button), correct the test under ADR-133. Keep its meaning and coverage, and note it in the Evidence line. Never skip, xfail or delete tests to get green.
- Live tests (`-m live`, `JOBPIPELINE_LIVE_TESTS=1`) are user-run. Don't run them; list them as left for the user.

## 5. Plan of attack

**Phase 0: orient (short).** Do section 0 first. Then run `git show --stat HEAD` to see what the WIP commit holds, read the last two session log entries, run `next_task.py --open`, and skim review-notes. Check whether `app/static/css/app.css` already holds the 600 px top-bar fix (the Search button label must not wrap the top bar at 600 px: icon-only below the breakpoint, or otherwise compact).

**Phase 1: flakes.** Already started (see section 0): `tests/e2e/test_swap_binding.py` is a failing-first test for TD-320, and `test_m7_cross_cutting.py` was edited for TD-321. Finish them. Fix TD-320 and TD-321 properly. For TD-320, a settle/bind wait in the e2e helpers (`tests/e2e/` helpers, `tests/waiting.py`) and/or a fix that keeps forms from submitting natively before htmx binds. Look at the app side too: a form that can submit without htmx is a real bug. Close both in TECH_DEBT.

**Phase 2: M9.5 part A (palette).** Implemented and passing its own tests, not yet reviewed. Finish what's there: the 600 px fix, a full e2e pass without `-x`, fixing regressions from the new button and dialog, and the ADR-138 choices. Security matters here: the query is never logged, the fragment needs the token and writes nothing, recent paths are validated as safe ids only, and all output is escaped.

**Phase 3: M9.5 part B (global shortcuts).** Drafted in `app/static/js/shortcuts.js`, `fragments/shell/_shortcuts.html` and `_shortcut_list.html`, with `tests/e2e/test_global_shortcuts.py`. Make it pass and complete it against the AC. G then T/I/B/F, N for quick add, `?` lists all shortcuts on every page, Esc closes a drawer or dialog. Single-key shortcuts respect the existing on/off switch. Ctrl/Cmd+K always works. Follow the design's shortcut table and the htmx-ui skill, keyboard first, phone width included.

**Phase 4: M9.5 part C (demo dataset).** Drafted: `app/demo_data/workspace.json` (built by `tests/demo_builder.py`), `app/services/demo.py` (seeded from `prepare()` in `app/main.py`), and `tests/integration/test_demo_seed.py`, `tests/demo_audit_child.py`, `tests/e2e/test_m9_demo.py`. Check it against every requirement below rather than assuming it's complete. A fictional persona, roles, facts, skills, about 15 jobs across all triage states, companies, applications across phases, events, and one practice CV template, shipped as an M9.1 export file in `app/demo_data/`. On `--demo`, seed once, through the importer, only when the folder is empty **and** `is_demo_workspace` is true (refuse otherwise, ADR-049). Shift dates from the injected clock so the anchor date becomes today. Use the demo keychain service. The AC-M9.4 test must prove (subprocess plus audit hook) that the real data and config folders are never touched. This is the most safety-sensitive part of the run.

→ Then: one independent review pass on all of M9.5 (section 7), fix findings, `verify.sh full`, update PROGRESS (tick, Evidence), commit.

**Phase 5: M9.6.**
- (A) Page inventory from the registered page routes. Every page gets an axe case over the demo dataset (light and dark, desktop and phone) and a keyboard walk to its primary action. Use parametrized tests so this stays one test file and not hundreds of hand-written ones. Fix what axe finds.
- (B) Performance at 1,000 jobs, 300 facts, 500 applications, 1,000 bank questions and 20 removal rules: every fragment under 150 ms (median of 5). Generate the scale data with a fast fixture builder, not through the UI. Time export and import and record the timings in ADR-138 (or a separate ADR if they're substantial). Fix slow queries with indexes or query changes. A schema change means a new forward-only migration, per `.claude/rules/database.md` and the `db-migration` skill.
- (C) Release hardening: `force-include` `prompts/` and `app/demo_data/` in the wheel, `PROMPTS_DIR` resolving in both the installed and the repo layout, `scripts/check_wheel.py` (runs `uv build --wheel` and inspects it, no new dependency) wired into `verify.sh full` and CI, version `1.0.0`, TD-111 accepted for v1.0, TD-115 documented (leave the Windows code in place), no Python 3.15.
- (D) README for a newcomer (prerequisites, install, run, every CLI option matching `--help`, demo, backup and restore, delete all data, troubleshooting), `docs/user-guide.md`, the spec sections listed in the task, TECH_DEBT closures, and `docs/acceptance/M9.md` (the VoiceOver/Orca manual checklist, README newcomer review, the TD-001 dependency advisory lookup, M5–M8 carry-over, **all unchecked**).

→ Then: review pass, fixes, `verify.sh full` on macOS, Linux full verification in Docker if available, update PROGRESS, commit.

## 6. Commits and bookkeeping

- Branch: `m9-polish-v1`. Stay on it. The WIP handoff commit stays in history; your task commits go on top of it. Don't push unless the user tells you to.
- Commits (conventional, one per task, plus at most one for flakes), for example:
  - `fix(e2e): settle htmx before key presses; stable random ids (TD-320, TD-321)`
  - `feat(palette): command palette, global shortcuts and demo dataset (M9.5)`
  - `chore(release): accessibility audit, performance, wheel, docs, v1.0.0 (M9.6)`
- Don't bypass pre-commit hooks. Commit only after a `VERIFY full: PASS` for that tree. Paste the PASS line and the log path into the Evidence line.
- `docs/PROGRESS.md` is parsed by `scripts/next_task.py`. **Preserve its format exactly** (checkbox lines, `- AC:`/`- Tests:`/`- Evidence:`/`- Checkpoint:` sub-bullets, the `Status:` line). Run `python3 scripts/next_task.py` afterwards to confirm it still parses. Add one concise session log entry per commit, not an essay.
- **Don't** set M9 `Status: accepted`. Acceptance is the user's decision.
- Add recurring review findings to `docs/development/review-notes/MEMORY.md` only if they're new.

## 7. Independent review (once per task, batched)

After each task's implementation, spawn independent reviewers in parallel on that task's diff: **code review** always, **security review** (M9.5: palette query and token, recent-path validation, escaping, demo seeding beside the real folders; M9.6 part C: wheel contents, no secrets or personal data packaged), and **UX/accessibility review** (templates, static files, shortcuts, phone width, WCAG 2.2 AA). Use the Codex agent profiles in `.codex/agents/` if available; otherwise spawn generic reviewers with the same brief (task AC, relevant rules, diff scope, test evidence). Fix high and medium findings. Record low findings as tech debt. One re-review is enough, and only for security-relevant fixes. If you can't delegate a review, say so plainly in the Evidence line and the final report. Don't call self-review independent.

## 8. Environment notes

- macOS, Python 3.11+, `uv`. Commands: `uv sync`, `uv run jobpipeline --demo --data-dir <tmp> --no-browser`, `uv run playwright install chromium` if browsers are missing.
- Never read `.env` files, keychain values or the user's real data/config folders. Always use a temporary `--data-dir`.
- Long runs: in the background, logs in a temp folder. On macOS wrap them in `caffeinate -dims ...`.
- `--demo` seeds only the folder the app picks for the demo (`is_demo_workspace`: `--demo` without `--data-dir`). With `--data-dir` nothing is seeded, by design. Tests point that folder at a temp location. Follow how `tests/e2e/test_m9_demo.py` and `tests/demo_audit_child.py` do it, and never run `--demo` against your real home folders by hand.

## 9. Final report to the user

End with a short report:
1. **Outcome:** v1.0 candidate ready or not, the final `VERIFY full` line, and the commit hashes.
2. **Decisions made on the user's behalf** (summary of ADR-138), so they can veto any.
3. **Test corrections** made under ADR-133, one line each.
4. **Left for the user:** review the README as a newcomer, run the manual checks in `docs/acceptance/M9.md` (including the M5–M8 carry-over), run the opt-in live tests, Linux verification if it didn't run, accept M9, and create the tag with `git tag -a v1.0.0 -m "v1.0.0"` (local, not pushed).
5. **Known issues:** anything recorded as new tech debt, and anything that didn't work.

If you made a mistake, say so plainly in the report. The user would rather have a finished app with a clear list of apologies than a half-finished app with perfect ceremony.
