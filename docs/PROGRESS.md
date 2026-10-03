# Progress

Current milestone: M0

How to use this file: `/plan-milestone M<n>` writes a milestone's tasks here, `/next-task` works through them, and `docs-keeper` ticks each task with evidence. Scripts parse this format, so keep it: a `Current milestone:` line, `## M<n> — Title` headings, and tasks written as `- [ ] M<n>.<k> Title` with indented `AC:`, `Tests:` and `Evidence:` lines.

## M0 — Foundation and tooling

Status: planned (draft plan; confirm with `/plan-milestone M0` before starting)

- [ ] M0.1 Project scaffold and tooling
  - AC: `uv sync` works on a fresh clone; ruff, mypy and pytest run with the markers from spec 7.2; pre-commit runs ruff and gitleaks.
  - Tests: a package import test; manual check that gitleaks blocks a staged file containing a fake key.
- [ ] M0.2 Module boundary contracts
  - AC: AC-M0.6; import-linter contracts encode spec 2.3.
  - Tests: contract self-test with a deliberately violating fixture module.
- [ ] M0.3 Domain skeleton, module protocols and fakes
  - AC: protocols from spec 2.4 exist; `Clock` and `IdFactory` are injectable; fakes live in `tests/fakes/`.
  - Tests: unit tests for fakes' basic behavior.
- [ ] M0.4 Bootstrap config and data folder resolution
  - AC: precedence CLI > env > bootstrap config > defaults; demo mode uses its own folder.
  - Tests: table-driven unit tests for precedence.
- [ ] M0.5 Database and migrations
  - AC: AC-M0.4.
  - Tests: migration ordering, idempotency, backup before upgrade, fresh-vs-upgraded schema equality.
- [ ] M0.6 App factory, CLI and health endpoint
  - AC: AC-M0.1; server binds to 127.0.0.1 only.
  - Tests: CLI flag parsing; integration test for `/health`.
- [ ] M0.7 Security middleware
  - AC: AC-M0.2, AC-M0.3.
  - Tests: integration tests for host, token and origin cases.
- [ ] M0.8 Logging with redaction
  - AC: AC-M0.5.
  - Tests: unit tests that push secrets and token-shaped strings through the logger.
- [ ] M0.9 App shell
  - AC: base template with vendored htmx, light and dark tokens, responsive shell with collapsing sidebar.
  - Tests: e2e load at desktop and 390x844, axe with no serious or critical violations.
- [ ] M0.10 Verification and CI
  - AC: AC-M0.7; smoke test; `scripts/check_critical_coverage.py`; GitHub Actions workflow per spec 4.6.
  - Tests: the verify script passes locally; CI green on three operating systems.

## M1 — Secrets, AI provider and the setup guide

Status: not planned (run `/plan-milestone M1`)

## M2 — Task runner, live updates and usage budgets

Status: not planned

## M3 — Fact bank and CV import

Status: not planned

## M4 — Job intake, inbox and companies

Status: not planned

## M5 — Applications board, tracking and Today

Status: not planned

## M6 — Tailoring, honesty enforcement and documents

Status: not planned

## M7 — Enrichment sessions and coverage

Status: not planned

## M8 — Interview preparation

Status: not planned

## M9 — Polish and version 1.0

Status: not planned

## M10 — Automated job sourcing

Status: not planned

## Session log

- 2026-10-03: Specs, references and the agentic system created. No application code yet.
