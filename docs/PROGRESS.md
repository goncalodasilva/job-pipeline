# Progress

Current milestone: M0

How to use this file: `/plan-milestone M<n>` writes a milestone's tasks here, `/next-task` works through them, and `docs-keeper` ticks each task with evidence. Scripts parse this format, so keep it: a `Current milestone:` line, `## M<n> — Title` headings, and tasks written as `- [ ] M<n>.<k> Title` with indented `AC:`, `Tests:` and `Evidence:` lines.

## M0 — Foundation and tooling

Status: planned

Branch: `m0-foundation`. Decisions for this plan: ADR-022 to ADR-032 (dependencies in ADR-031, CI operating systems in ADR-032).

- [x] M0.1 Project scaffold, dependencies, pytest markers
  - AC: AC-M0.7 (part); `uv sync` works on a fresh clone; ruff, mypy (strict, `app`), pytest with `--strict-markers` and the markers from spec 7.2; empty package tree from spec 2.13; `jobpipeline --version`; approved dependency set recorded in an ADR.
  - Tests: unit `test_package_exposes_version_from_metadata`, `test_markers_registered_and_strict` (pytester), `test_live_tests_skipped_without_env`, `test_live_tests_run_with_env`, `test_cli_version_prints_package_version_and_exits_0`.
  - Evidence: 2026-10-03, `verify.sh quick` PASS (format, lint, mypy strict on 19 files, 28 fast tests; smoke has no tests yet). Fresh clone: `uv sync --locked` OK, `jobpipeline --version` prints `jobpipeline 0.1.0`, 28 passed. Added after review: `test_unmarked_tests_named_live_are_not_skipped` (live gating now uses the marker, not keywords) and `test_cli_console_script_points_to_main`; pytest uses `--import-mode=importlib`. Ruff excludes `.claude/` and `docs/` (existing hook scripts and spec code samples are not project code); formatting the hooks is left to the user.
- [x] M0.2 Spike: import-linter contracts and self-test
  - AC: AC-M0.6; contracts encode spec 2.3 (domain purity, modules never import services/api/onboarding/tasks, services free of web frameworks and providers, api free of modules and sqlite3, composition root not imported by layers, layers contract). If forbidding not-yet-installed vendor packages errors, those contracts are added in the milestone that installs the package (ADR), never weakened.
  - Tests: integration `test_ac_m0_6_lint_imports_passes_on_repo`; `test_ac_m0_6_violation_detected[<contract>]` with one violating fixture per contract (stored as `.py.txt`, run in a temp copy).
  - Evidence: 2026-10-03, `verify.sh quick` PASS (format, lint, types, module boundaries: 9 contracts kept; 80 fast tests). `test_ac_m0_6_lint_imports_passes_on_repo`, `test_ac_m0_6_violation_detected[...]` (15 cases, ids by case), `test_every_forbidden_entry_is_enforced[...]` (every forbidden entry), `test_legal_layered_import_chain_keeps_every_contract`, `test_vendor_sdks_allowed_inside_their_provider_packages`, `tests/unit/test_domain_purity.py`. Spike findings and design in ADR-033. The task-runner question was settled in ADR-034: services only.
- [x] M0.3 Spike: pre-commit with ruff and gitleaks
  - AC: AC-M0.7 (part); `.pre-commit-config.yaml` with pinned ruff-format, ruff check and gitleaks; tests build key-shaped strings at runtime, no gitleaks allowlist.
  - Tests: unit `test_precommit_config_has_pinned_ruff_and_gitleaks`; manual check that a staged file with a fake key is blocked (recorded as evidence).
  - Evidence: 2026-10-03, `verify.sh quick` PASS (86 fast tests); `pre-commit run --all-files` and `pre-commit run --hook-stage manual gitleaks-history` pass on the repo. Tests: `test_precommit_config_has_pinned_ruff_and_gitleaks`, `test_ruff_hook_version_matches_locked_ruff`, `test_ruff_honours_excludes_when_given_file_names`, `test_gitleaks_runs_without_allowlist`, `test_no_inline_gitleaks_allow_comments`, `test_gitleaks_history_hook_scans_commits`. Manual check in a throwaway clone: committing a staged file with a randomly generated fake Anthropic-shaped key was blocked (gitleaks RuleID `anthropic-api-key`, commit exit 1, no commit created); a fake key already committed was caught by `gitleaks-history` (exit 1). Review finding: the per-commit gitleaks hook scans only staged changes, so CI and `verify.sh full` use the `gitleaks-history` hook (manual stage). Install now includes `uv run pre-commit install` (CLAUDE.md, spec 4.5).
- [x] M0.4 Spike: Playwright and axe helper
  - AC: e2e helper `assert_no_serious_violations(page)` with vendored axe-core (MPL-2.0, license kept); desktop and 390x844 page fixtures; Chromium install proven in this environment.
  - Tests: e2e `test_axe_helper_flags_image_without_alt_and_low_contrast`, `test_axe_helper_passes_clean_page`.
  - Evidence: 2026-10-03, `verify.sh quick` PASS (89 fast tests); `JOBPIPELINE_E2E_CHROMIUM=/opt/pw-browsers/chromium uv run pytest -m e2e` 9 passed: `test_axe_helper_flags_image_without_alt_and_low_contrast`, `test_axe_helper_passes_clean_page[desktop_page|phone_page]`, `test_axe_helper_ignores_minor_and_moderate_violations`, `test_axe_helper_reports_violation_shape`, `test_axe_helper_runs_best_practice_and_wcag22_rules[best-practice|wcag22aa]` (checked to fail when the tags are removed), `test_desktop_page_viewport`, `test_phone_page_viewport_and_touch`; unit `test_vendored_axe_*` incl. `test_vendored_files_are_never_converted_by_git`. axe-core 4.13.0 tarball checked against the npm sha512 before vendoring. Chromium: Playwright 1.63's managed build isn't available in this sandbox, so the evidence used the pre-installed Chromium 141 via `JOBPIPELINE_E2E_CHROMIUM` (ADR-035); managed Chromium is proven by CI in M0.19.
- [x] M0.5 CI workflow skeleton
  - AC: AC-M0.7 (part); GitHub Actions lint job (format, lint, types, import contracts, and secrets via `pre-commit run --hook-stage manual gitleaks-history` on a full-history checkout, `fetch-depth: 0`) and fast-test job on Python 3.11 and 3.14; Ubuntu on every push; macOS and Windows on pull requests to main, manual dispatch and weekly (ADR-032).
  - Tests: unit `test_ci_matrix_covers_three_os_and_oldest_newest_python`; evidence: URL of a CI run.
  - Evidence: 2026-10-03, CI run https://github.com/goncalodasilva/job-pipeline/actions/runs/37144804054 green on push: lint job (format, lint, mypy, lint-imports, `gitleaks-history` over full history) and fast tests on Ubuntu with Python 3.11 and 3.14; macOS/Windows job skipped on push as designed (ADR-032). `verify.sh quick` PASS (96). Tests: `test_ci_matrix_covers_three_os_and_oldest_newest_python`, `test_ci_runs_macos_and_windows_only_off_push`, `test_ci_lint_job_runs_all_static_checks_and_history_secret_scan`, `test_ci_actions_pinned_to_commit_shas`, `test_ci_has_read_only_permissions`, `test_ci_concurrency_never_cancels_scheduled_or_dispatched_runs`, `test_ci_jobs_have_timeouts_and_drop_checkout_credentials`. Note: manual dispatch and the weekly schedule only work once the workflow is on `main`; before that, the three-OS run for AC-M0.7 comes from a (draft) pull request to `main` (M0.19, with the user's go-ahead).
- [x] M0.6 Critical coverage script
  - AC: spec 4.3; `scripts/check_critical_coverage.py` enforces 95% branch coverage per critical module using the registry from ADR-028 (module, milestone); existing file below threshold or unmeasured fails; missing file fails once its milestone is current or past at acceptance (`--milestone`), otherwise reported as not yet present; registry matches spec 4.3.
  - Tests: unit with JSON fixtures: `test_passes_when_all_present_modules_at_or_above_95_branch`, `test_fails_and_names_module_below_threshold`, `test_existing_critical_file_missing_from_report_fails`, `test_future_module_absent_reported_not_failed`, `test_module_due_by_milestone_but_absent_fails`, `test_threshold_uses_branch_not_line_coverage`, `test_registry_matches_spec_section_4_3`.
  - Evidence: 2026-10-03, `verify.sh quick` PASS; 20 tests in `tests/unit/test_check_critical_coverage.py` (the seven named above plus Windows paths, invalid milestone, fail-closed without branch data, absolute path outside root, default report built from `.coverage` from any working directory with real measured coverage, missing data, unreadable report); `mypy --strict` clean on the script. On the repo: no flag → all seven "not yet present", exit 0; `--milestone M0` → middleware and runner fail "due by M0" until M0.13/M0.14. `/milestone-acceptance` now runs the script with `--milestone M<n>`.
- [x] M0.7 Domain skeleton: Clock, IdFactory, errors, fakes
  - AC: `Clock` and `IdFactory` protocols with `SystemClock` (aware UTC) and `UuidIdFactory`; spec 2.11 error classes; `FrozenClock` and `SequentialIdFactory` in `tests/fakes/`.
  - Tests: unit `test_system_clock_returns_aware_utc`, `test_frozen_clock_advance`, `test_sequential_ids_are_deterministic`, `test_uuid_factory_ids_unique`, `test_no_wall_clock_or_uuid_calls_outside_clock_and_ids` (AST scan of `app/`).
  - Evidence: 2026-10-03, `verify.sh quick` PASS (183 fast tests). Named tests plus: mypy `--strict` conformance of `SystemClock`, `FrozenClock`, `UuidIdFactory`, `SequentialIdFactory` against the protocols (and a wrong-signature clock rejected); spec 2.11 messages per error; `detail` never in `user_message`; locale-independent reset dates ("1 November 2026"); the scanner flags any reference (including `default_factory=datetime.now`, `uuid7`, implicit-now `time` functions) with 23 self-test snippets and allows `time.monotonic`, `perf_counter` and `secrets`. Copy decision: the generic `AppError` fallback reads "Something went wrong. Try again." (not in the spec 2.11 table).
- [x] M0.8 Module interfaces from spec 2.4
  - AC: interface types only (ADR-026) for ai, search, calendar, secrets and fetch; implementations and contract suites come in their milestones.
  - Tests: unit `test_ai_request_defaults_no_web_search_and_no_hook`, `test_ai_capabilities_default_false`, `test_dataclasses_are_frozen[...]`, `test_protocol_conformance` (mypy over conforming dummies).
  - Evidence: 2026-10-03, `verify.sh quick` PASS (207 fast tests); 24 tests in `tests/unit/modules/test_module_interfaces.py`: the named ones plus spec 2.9 fetch limits, event time and uid validation, mypy `--strict` acceptance of narrowed and property-based implementations, and rejection of three mismatches; no vendor SDK imported. Refinements to spec 2.4 recorded in ADR-036 (fetch behind an injected `JobPageFetcher`, read-only protocol members, `EventData.uid`).
- [x] M0.9 Runtime config and data folder resolution
  - AC: precedence CLI > env > bootstrap config > defaults; `JOBPIPELINE_CONFIG_DIR` overrides the config folder (ADR-024); demo uses `user_data_dir("jobpipeline-demo")` and `--data-dir` wins over `--demo` (ADR-025); malformed bootstrap config gives a plain error.
  - Tests: unit table-driven `test_precedence_cli_env_bootstrap_default[data_dir|port|log_level]`, `test_demo_uses_its_own_folder_and_ignores_bootstrap_data_dir`, `test_data_dir_wins_over_demo`, `test_invalid_port_env_raises_config_error`, `test_missing_bootstrap_is_none`, `test_malformed_bootstrap_raises_config_error`, `test_config_dir_override_env`.
  - Evidence: tests/unit/test_config.py (72 cases, includes test_precedence_cli_env_bootstrap_default[data_dir|port|log_level], test_demo_uses_its_own_folder_and_ignores_bootstrap_data_dir, test_data_dir_wins_over_demo, test_invalid_port_env_raises_config_error, test_missing_bootstrap_is_none, test_malformed_bootstrap_raises_config_error, test_config_dir_override_env); VERIFY quick PASS (279 passed, ruff, mypy, 9 import contracts kept); code-review approved, review fixes applied.
- [x] M0.10 Logging with redaction
  - AC: AC-M0.5; JSON formatter; rotating files (5 x 1 MB); redaction filter on handlers so uvicorn logs pass through it; no module-level mutable state.
  - Tests: unit `test_ac_m0_5_registered_secret_redacted_in_message_args_extra_and_traceback` (including an `AppError` whose `detail` holds a secret), `test_ac_m0_5_anthropic_key_and_oauth_shapes_redacted[api03|oat01]`, `test_ac_m0_5_secret_embedded_in_arbitrary_text_redacted` (Hypothesis), `test_ac_m0_5_uvicorn_access_log_redacted`, `test_json_formatter_fields`, `test_rotation_1mb_5_files`.
  - Evidence: 2026-10-03, `verify.sh quick` PASS (313 fast tests; ruff, mypy, 9 import contracts kept); tests in tests/unit/domain/test_redaction.py and tests/unit/test_logging_config.py, including test_ac_m0_5_registered_secret_redacted_in_message_args_extra_and_traceback, test_ac_m0_5_anthropic_key_and_oauth_shapes_redacted[api03|oat01], test_ac_m0_5_secret_embedded_in_arbitrary_text_redacted (Hypothesis), test_ac_m0_5_uvicorn_access_log_redacted, test_json_formatter_fields, test_rotation_1mb_5_files, plus review regressions (bad format call doesn't raise, escaped secret forms redacted, extras can't overwrite core fields, warnings captured, log folder 0700 and files 0600 on POSIX); Code review and security review done; fixes applied.
- [x] M0.11 Database and migration runner
  - AC: AC-M0.4; WAL, foreign keys, busy timeout; `transaction()` with `BEGIN IMMEDIATE`; numbered migrations applied in order, each in its own transaction, recorded in `schema_migrations`; backup with the sqlite3 backup API before pending migrations on an existing database, last 5 kept.
  - Tests: integration `test_ac_m0_4_empty_db_gets_schema`, `test_ac_m0_4_rerun_is_noop_and_makes_no_backup`, `test_ac_m0_4_existing_db_backed_up_before_new_migration`, `test_backup_includes_uncheckpointed_wal_rows`, `test_backups_keep_last_five`, `test_migrations_apply_in_numeric_order`, `test_duplicate_migration_number_rejected`, `test_failed_migration_rolls_back_and_is_not_recorded`, `test_pragmas_wal_fk_busy_timeout`, `test_transaction_rolls_back_on_exception`.
  - Evidence: 2026-10-03, `verify.sh full` PASS (338 fast tests with coverage, critical coverage, 9 e2e, pre-commit incl. gitleaks; smoke has no tests yet); 25 integration tests in tests/integration/test_storage_database.py: the 10 named ones plus misnamed .sql rejected, migration ending its own transaction not recorded, failed COMMIT rolls back, threads take turns in transactions, unknown or out-of-order versions rejected, keep_backups >= 1, pruning leaves other files and failed backups leave no partial, database/WAL/backups 0600 and folders 0700 on POSIX. Code and security reviews done; fixes applied.
- [x] M0.12 Migration 0001 and schema equality
  - AC: AC-M0.4; `0001_initial.sql` with schema_migrations, settings, setup_state, tasks and usage_records; every mutable table has `created_at`, `updated_at` and `version` (ADR-027); no FTS tables, dedupe index or `usage_records.task_id` yet (M2). The runner creates `schema_migrations(version, name, applied_at)` itself (ADR-038); 0001 declares it with `IF NOT EXISTS` and the same columns.
  - Tests: integration `test_ac_m0_4_schema_fresh_equals_upgraded_step_by_step`, `test_0001_creates_expected_tables`, `test_mutable_tables_have_created_updated_version`, `test_tasks_status_check_rejects_unknown_state`, `test_settings_value_must_be_json`, `test_sqlite_build_supports_fts5_and_json`.
  - Evidence: 2026-10-03, `verify.sh full` PASS (350 fast tests with coverage, critical coverage, 9 e2e, pre-commit incl. gitleaks; smoke has no tests yet); 12 integration tests in tests/integration/test_schema.py: the named ones plus test_setup_state_status_check, test_0001_sql_declares_schema_migrations_if_not_exists and test_0001_schema_migrations_matches_the_runner_table. Code review done; fixes applied (dropped a speculative plain index on tasks.dedupe_key; M2 adds the unique partial one).
- [x] M0.13 App factory, lifespan, task runner stub, /health
  - AC: AC-M0.1 (part); lifespan opens the database, applies migrations, starts and stops the runner stub; `/health` returns version, database and runner status, 503 when the database is not ok (ADR-023). All database access from request handlers and the task runner goes through `Database.transaction()` (it holds a lock; the `connection` property is for start-up and tests only, ADR-038).
  - Tests: integration `test_health_returns_version_database_ok_runner_running`, `test_lifespan_creates_db_and_applies_migrations_in_tmp_data_dir`, `test_shutdown_stops_runner_and_closes_db`, `test_health_reports_database_error_with_503`; unit `test_runner_stub_start_stop_idempotent`.
  - Evidence: 2026-10-03, `verify.sh full` PASS (378 fast tests with coverage, critical coverage, 9 e2e, pre-commit incl. gitleaks; smoke has no tests yet); tests: integration tests/integration/test_app.py (the 4 named plus lifespan state, payload has exactly version/database/runner, migration failure at startup raises and closes the database), unit tests/unit/tasks/test_runner.py (test_runner_stub_start_stop_idempotent, restart, stop before start, idle worker, cancelling stop is not swallowed) and tests/unit/services/test_health.py (ok, closed database reports error, logs only the error type, query inside a transaction). Code review done; fixes applied.
- [x] M0.14 Security middleware (host, token, origin)
  - AC: AC-M0.2, AC-M0.3; host check, launch token (`X-JP-Token`, constant-time compare) on everything except page GETs, static files and `/health` (ADR-023), origin or referer check on state-changing requests.
  - Tests: integration `test_ac_m0_2_foreign_host_rejected[...]`, `test_ac_m0_2_loopback_hosts_allowed[...]`, `test_ac_m0_3_fragment_without_token_403`, `test_ac_m0_3_post_without_token_403`, `test_ac_m0_3_wrong_token_403`, `test_ac_m0_3_post_with_token_and_origin_handled`, `test_post_origin_mismatch_403`, `test_post_referer_fallback_ok`, `test_post_without_origin_or_referer_403`, `test_origin_null_403`, `test_page_get_without_token_ok`, `test_health_and_static_without_token_ok`; unit `test_launch_token_32_bytes_urlsafe_unique_per_app`.
  - Evidence: 2026-10-03, `verify.sh quick` PASS (457 fast tests; ruff, mypy, 9 import contracts kept); tests/integration/test_security_middleware.py: the 12 named tests plus host check before token, repeated headers refused, untagged GET without `HX-Request` still needs the token, `/api` and unknown paths need the token, page route with `HX-Request` needs the token, port 80, generated API docs not served, protective headers on every response including rejections, websocket refused; tests/unit/api/test_security.py: `test_launch_token_32_bytes_urlsafe_unique_per_app`. Code and security reviews done; fixes applied; protective headers approved by the user (ADR-039).
- [x] M0.15 CLI flags, 127.0.0.1 binding, browser opening
  - AC: AC-M0.1 (part); `--port`, `--data-dir`, `--no-browser`, `--setup` (stored only, ADR-030), `--demo`, `--version`; host fixed to 127.0.0.1; browser opened once after `/health` responds; friendly port-in-use message. Map the flags to `CliOptions` (app/config.py, M0.9); print a `ConfigError`'s `detail` (it names the setting, never its value), because `user_message` is fixed generic text and config resolves before logging exists. Logging (M0.10): call `configure_logging` first, before anything logs or reads secrets; start uvicorn with `host="127.0.0.1"`, `proxy_headers=False`, `log_config=None` and no root path (ADR-039), and test this with a real `uvicorn.Config`; route `sys.excepthook` and `threading.excepthook` through a logger. Startup failures (for example `MigrationError` from the lifespan) print a plain console message and exit non-zero instead of a raw traceback.
  - Tests: unit `test_cli_parses_all_flags`, `test_cli_binds_127_0_0_1_only`, `test_cli_has_no_host_flag`, `test_cli_opens_browser_once_after_health_200`, `test_cli_no_browser_never_opens`, `test_cli_port_in_use_prints_hint_and_exits_1`, `test_cli_setup_flag_sets_force_setup`, `test_cli_demo_selects_demo_data_dir`.
  - Evidence: 2026-10-03, `verify.sh quick` PASS (489 fast tests; ruff, mypy, 9 import contracts kept); 30 tests in tests/unit/test_cli.py (the 8 named ones plus flag mapping, no --host flag, real uvicorn.Config and loggers, health give-up, ConfigError exits 2 with the detail, start-up failures modelled on real uvicorn exit 1 without a traceback, unexpected error and data-dir-is-a-file print plain lines, Ctrl+C exits 130, browser never opens after a failed start, port free/taken, proxy-free health probe, and one end-to-end test with real uvicorn whose lifespan fails) and 4 new tests in tests/unit/test_logging_config.py (console omits tracebacks, exception hooks). Manual run: /health 200 with protective headers, foreign Host 400, second instance on the same port prints the hint and exits 1, bad JOBPIPELINE_PORT exits 2 with one line, Ctrl+C exits 0, data/logs/app.db 0700/0700/0600, a failing start-up prints plain lines and exits 1. Code and security reviews done; fixes applied (ADR-040).
- [ ] M0.16 Running-app fixture and smoke test
  - AC: AC-M0.1, AC-M0.5 end to end; subprocess fixture with temp data and config folders that works on Windows too. Start the app in the subprocess with `sys.executable` and the project on `PYTHONPATH` (or `python -m app`), not the console script: on macOS the venv's `.pth` files can carry the `hidden` flag, which Python 3.14 skips.
  - Tests: smoke `test_ac_m0_1_health_200_within_5_seconds`, `test_smoke_root_page_200`, `test_ac_m0_5_launch_token_not_in_log_files`.
- [ ] M0.17 App shell templates, vendored htmx, page routes
  - AC: base template with vendored htmx 2.0.x, skip link, sidebar in design order (Today, Job inbox, Board, Fact bank, Companies; Settings pinned), top bar placeholders, live region, token in `hx-headers`; placeholder empty-state page per future area (ADR-029); fragment vs page rendering. Register page routes with `tags=[PAGE_TAG]` (others need the token, ADR-039); when a request gets 403 after an app restart (stale token), show a plain "Reload the page" message in the live region.
  - Tests: integration `test_root_renders_shell_with_sidebar_in_design_order`, `test_page_embeds_launch_token_in_body_hx_headers`, `test_vendored_htmx_served_and_matches_pinned_version`, `test_render_returns_fragment_for_hx_request`, `test_autoescape_on_for_templates`, `test_pages_send_no_store`, `test_placeholder_pages_have_empty_state`.
- [ ] M0.18 CSS tokens, responsive sidebar, e2e and axe
  - AC: light and dark tokens; responsive shell with collapsing sidebar operable by keyboard; visible focus; 24 px targets; reduced motion respected.
  - Tests: e2e `test_shell_desktop_sidebar_visible`, `test_shell_phone_390x844_sidebar_collapsed_and_opens_by_keyboard`, `test_no_horizontal_scroll_at_360px`, `test_dark_scheme_changes_background_token`, `test_axe_no_serious_or_critical[desktop-light|desktop-dark|phone-light|phone-dark]`, `test_nav_targets_at_least_24px`.
- [ ] M0.19 CI completion, full verification, docs
  - AC: AC-M0.7; smoke, e2e and coverage jobs in CI; README and CLAUDE.md commands updated; verify.sh coverage run aligned with spec 4.5; mypy also type-checks `scripts/` (`files = ["app", "scripts"]`, bare `uv run mypy` in verify, CI, CLAUDE.md and spec 4.5). README troubleshooting: if `jobpipeline` fails with `No module named 'app'` on macOS, run `chflags nohidden .venv/lib/python3.*/site-packages/*.pth` or recreate the venv.
  - Tests: unit `test_ci_has_smoke_e2e_and_coverage_jobs`; `verify.sh full` passes; CI run green on Ubuntu, macOS and Windows (URL as evidence).


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
- 2026-10-03: M0 planned (19 tasks) with `/plan-milestone M0`; decisions recorded as ADR-022 to ADR-030. Dependency list and CI operating-system policy still awaiting the user.
- 2026-10-03: Dependency set (ADR-031) and CI policy (ADR-032) approved; pushing to `m0-foundation` allowed.
- 2026-10-03: M0.1 done: scaffold, approved dependencies, pytest markers and live-test gating, `jobpipeline --version`.
- 2026-10-03: M0.2 done: nine import-linter contracts with self-tests (ADR-033).
- 2026-10-03: M0.3 done: pre-commit with SHA-pinned ruff and gitleaks, plus a history scan for CI and full verification.
- 2026-10-03: M0.4 done: vendored axe-core, accessibility helper, desktop and phone browser fixtures (ADR-035).
- 2026-10-03: M0.5 done: CI workflow; first run green on Ubuntu.
- 2026-10-03: M0.6 done: critical coverage script with milestone-aware registry (ADR-028).
- 2026-10-03: M0.7 done: Clock, IdFactory, user-facing errors, FrozenClock and SequentialIdFactory fakes.
- 2026-10-03: M0.8 done: interface types for the ai, search, calendar, secrets and fetch modules (ADR-036).
- 2026-10-03: M0.9 done: runtime config resolution with CLI/env/bootstrap/defaults precedence, bootstrap config validation, demo folder, ConfigError handling.
- 2026-10-03: M0.10 done: JSON logging with rotating files and a redaction filter on handlers (ADR-037).
- 2026-10-03: M0.11 done: SQLite database wrapper and forward-only migration runner with backups (ADR-038).
- 2026-10-03: M0.12 done: migration 0001 (settings, setup_state, tasks, usage_records) and schema-equality test.
- 2026-10-03: M0.12 follow-up (user request): migration hash check, and a one-time approved edit of 0001 adding `typeof` checks to whole-number columns; `verify.sh full` PASS (360 fast tests, 9 e2e).
- 2026-10-03: M0.13 done: create_app with lifespan (database, migrations, runner stub) and GET /health.
- 2026-10-03: M0.14 done: host, launch-token and origin guards with protective headers (ADR-039).
- 2026-10-03: M0.15 done: jobpipeline command with flags, loopback-only uvicorn, browser opening and plain start-up errors (ADR-040).
