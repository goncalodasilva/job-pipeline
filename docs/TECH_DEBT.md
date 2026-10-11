# Technical debt register

Status as of 2026-10-11 (M0 to M3.2 and the M2 and M1 acceptance reports).

## What belongs here

Every known technical debt item, known limit, deferred hardening step and unverified item that is still open. It holds what would otherwise live only in Evidence lines or in conversations.

- Add an entry when a task leaves something behind. Use the next free id.
- Remove the entry in the commit that fixes it. Do not renumber; gaps are fine.
- Decisions stay in `docs/DECISIONS.md`. An entry here is work still owed. If an entry needs a decision, record it in a new ADR.
- Items closed by a later task are not listed. Entries marked "(status unclear)" could not be confirmed as open or closed. Check them first.
- Impact is high, medium or low, with a few words on why. Sections are ordered by impact.

Sources swept: every Evidence line and "Left for the user" note in `docs/PROGRESS.md` (M0 to M3.2); `docs/acceptance/M0.md`, `M1.md`, `M2.md`; `docs/live-check-m1.md`; the consequences and open questions of the ADRs; spec section 5 and section 7.5; and a search of `app`, `scripts` and `tests` for TODO, FIXME and XXX (none found).

## 1. Security and hardening

| ID | Item | Origin | Impact | When / fix |
| --- | --- | --- | --- | --- |
| TD-001 | Advisories for pypdf 6.20.0 and lxml 6.1.3 have not been checked. No dependency audit runs in `verify.sh` or CI. Both libraries parse untrusted files. | M3.1 Evidence (Left for the user, b) | high: both parse uploaded CVs; a known flaw would go unnoticed | Run an audit before M3 acceptance. A CI audit step needs your approval. |
| TD-002 | `lxml` (compiled, bundles libxml2) comes only through python-docx. No import contract names it, and it is not a listed dependency. | M3.1; ADR-089 update (M3.1) | medium: a native parser reads untrusted Word files. Mitigated by entity resolution off and parsing in a killed child | M3.4 review. List or pin it when the dependency set changes. |
| TD-003 | Values passed with `extra=` on Claude SDK loggers are stripped only by the app's handler filters. The pinned SDK passes none today. | M1.23 Left; ADR-070 | medium: a future SDK version could put a key in a record that no filter sees | Re-check on every SDK upgrade. |
| TD-004 | The app relies on SDK internals: a private `MessageParseError` import in the rate-limit parser, and tests that call `SubprocessCLITransport._build_command`. | ADR-078 (M2.2); ADR-045 | medium: an SDK upgrade can break parsing or tests late | Re-check on every SDK upgrade. The pin is exact (==0.2.163). |
| TD-005 | Token-free access to `/static/` is decided by a path-prefix check, not by the first matching route as pages are. | ADR-041 follow-up (a); M0 acceptance | low: static files only today | When pattern routes under `/static` arrive. |
| TD-006 | Every 403 shows the "restarted, reload" message, even if another cause produced it. | ADR-041 follow-up (b) | low: no other 403 case exists today | When another 403 case is added: key the message off a marker header. |
| TD-007 | No `script-src 'self'` Content-Security-Policy. The policy only sets frame-ancestors. | ADR-077 (out of scope) | low: wider hardening idea | Decide in a later security pass. |
| TD-008 | The snapshot `window` field accepts any string of the form `[A-Za-z0-9_.:-]{1,64}`. An allow-list per provider would close this. | ADR-088 item 6; M2.26 Known limits | low: Claude windows come from a fixed list, but a future provider could store odd text | When the next provider is added. |
| TD-009 | `ANTHROPIC_AUTH_TOKEN` is blanked for SDK calls but is not registered with the redactor. The decision on registering it was left open. (status unclear: no decision found) | ADR-047 follow-up; M1.23 | low: the value could reach a log only if a library echoes it | Decide and record in a new ADR. |

## 2. Privacy and stored data

| ID | Item | Origin | Impact | When / fix |
| --- | --- | --- | --- | --- |
| TD-010 | No erase path for fact text. Versions and events are append-only, facts cannot be deleted ("discarded" is the only removal), and removing a CV import keeps its drafts' text. Backups keep copies. | M3.2; ADR-090 item 3 and Consequences; M3.2 Left (a) | high: personal CV text cannot be removed from the app or its backups | A later migration and a design decision before any "delete my data" promise. M9 at the latest. |
| TD-011 | Soft-deleted capture notes and closed refine sessions keep their text. No purge runs. | M3.2; ADR-090 item 10; M3.2 Left (e) | high: personal text stays after the user deletes it | Same migration and decision as TD-010. |
| TD-012 | Snapshot privacy rule: snapshots are left out of diagnostics and exports unless the user opts in. `observed_at` shows when the user works. No exports or diagnostics exist yet, so the rule is not enforced. | ADR-078 Consequences | low: no export path yet | When exports (M6) or diagnostics (M9) are built. |

## 3. Schema and storage

| ID | Item | Origin | Impact | When / fix |
| --- | --- | --- | --- | --- |
| TD-013 | Date CHECKs on `facts` accept impossible dates such as 2026-02-30. The domain date validator is not written yet. | M3.2; ADR-090 Consequences | medium: bad dates can be stored by any path that skips the domain | M3.3: validate in the domain before any write. |
| TD-014 | Alias collisions for skills (JSON `aliases`) and JSON `tags` cannot be enforced in SQL. The service has to check them. | M3.2; ADR-090 item 9 | medium: duplicate skills can slip in through another path | M3.3: service check with tests. |
| TD-015 | `roles` has no `cv_import_id`. Removing an import leaves its draft roles behind as orphans. | M3.2 Left (c) | medium: orphan draft roles in the library | M3.3, or a new migration when roles are cleaned up. |
| TD-016 | `cv_imports` has no failure reason, file size or `failed` state. The failure text comes from the task row instead. | M3.2 Left (b) | low: the task row carries the reason | When the failed-import view needs more (M3.5). |
| TD-017 | No `source_quote` column. The CV quote of an imported unit is stored in `original_answer`. | M3.2 Left (d); ADR-090 item 8 | low: a misnamed field is easy to misuse | Add the column in a later migration when M3.4 needs it. |
| TD-018 | `needs_check` is a flag with no reason stored. | M3.2 Left (d) | low: the reason exists only in the check output | M3.8. |
| TD-019 | `refine_sessions` has no link to the accepted version. | M3.2 (design) | low: weaker trail from a refinement to the saved version | M3.8. |
| TD-020 | `cv_imports.task_id` and `refine_sessions.task_id` are not indexed. | M3.2 | low: slower lookups as data grows | When the lookup code needs it (M3.4). |
| TD-021 | The FTS5 index has no stemming, so "manage" does not match "managed". | M3.2 | low: search recall | Decide in M3.3 or later. |
| TD-022 | Duplicate inserts on the three guarded tables fail with the guard's message, not "UNIQUE constraint failed". | M3.2; ADR-090 Consequences | low: the service must match error types, not text | M3.3. |
| TD-023 | `word_diff` yields whitespace-only segments. The M3.6 diff must hide or label them. | M3.2 | low: display only | M3.6. |
| TD-024 | `fact_events` has no sequence column. History is ordered by `created_at, rowid`. | M3.2; ADR-090 item 3 | low: ties within one timestamp rely on rowid | When history order matters more (M3.6). |
| TD-025 | `budget_notices.cycle_start` has a format-only CHECK that accepts 2026-02-30. The only writer builds real dates. | M2.5 (ADR-078 update) | low: the only writer is correct | Tighten with a later migration. Committed migrations are never edited. |
| TD-026 | One shared SQLite connection, with an RLock so threads take turns. | ADR-038 | low: no contention reported | Revisit with per-thread connections if contention appears. |
| TD-027 | `Database` has no read-only open for test helpers. | M2.26 Known limits | low: helpers open the app database for writing | When a test helper needs it. |

## 4. Extraction and uploads

| ID | Item | Origin | Impact | When / fix |
| --- | --- | --- | --- | --- |
| TD-028 | Word headers, footers and text boxes are not extracted. Their content is silently missing from drafts. | M3.1 Left (d) | medium: CV content is dropped with no message | M3.4 decision: extract them, or tell the user. |
| TD-029 | Extracted text may keep C1 control characters and bidi override characters. | M3.1 Evidence | medium: misleading display and prompt text from untrusted files | M3.4: strip before storage or before prompts. Security and prompt review. |
| TD-030 | `extract_text` and `sniff_type` block. They must run in a worker thread, which M3.4 has to wire. | M3.1 Left (d) | medium: they stall the event loop if called directly | M3.4. |
| TD-031 | The parser child has no memory limit. If the app is killed hard, an orphaned child lives until its CPU limit ends it. | M3.1 Evidence | medium: a hostile file can use a lot of memory for a while | M3.4: address-space limit or a dedicated worker. |
| TD-032 | `read_upload` subclasses Starlette's semi-public `MultiPartParser`, and `app/api` imports `python_multipart` directly. No import contract covers that import. | M3.1 Evidence | medium: a Starlette upgrade can break uploads; the import sits outside the provider rule | Re-check on each Starlette upgrade. Decide whether a contract is needed. |
| TD-033 | No read timeout on slow upload bodies, and about three copies of the body are held in memory. | M3.1 Evidence | medium: a slow client holds a request and memory | M3.4 or M9 release hardening. |
| TD-034 | Each parser spawn re-imports the app (about 0.6 s), and that counts against the parse timeout. A dedicated `python -m` worker would avoid it. | M3.1 Left (d) | low: slower parses of large files | M3.4 or later. |
| TD-035 | A client disconnect during upload is reported as `UploadNotAccepted`. | M3.1 Evidence | low: wrong message in a rare case | When touched. |
| TD-036 | "10 MB" is written into the upload error message, separately from `MAX_UPLOAD_BYTES`. | M3.1 Evidence | low: the copy drifts if the limit changes | When touched. |
| TD-037 | A document whose extracted text exceeds 16 MiB gets the "may be damaged" message, not "too much text". | M3.1 Evidence | low: wrong message | When touched. |
| TD-038 | The `extractor=` test seam sits in the production signature of `extract_text`. | M3.1 Evidence | low: a test hook in the production API | M3.4 or when touched. |
| TD-039 | The Word merged-cell skip is untested and uses python-docx's private `_tc` attribute. | M3.1 Evidence | low: breaks silently on a python-docx upgrade | When python-docx is upgraded. |
| TD-040 | Word entry limits (16 MiB per entry, 32 MiB total) are looser than a CV needs. | M3.1 Evidence | low: bounds are wider than needed | When touched. |
| TD-041 | PDFs with bytes before `%PDF-` are refused. | M3.1 Evidence | low: some valid files are refused | Decide in M3.4. |
| TD-042 | Pasted text with a stray control character is refused as unsupported, not cleaned. | M3.1 Left (d) | low: friction for the user | M3.4 decision. |

## 5. AI provider and tasks

| ID | Item | Origin | Impact | When / fix |
| --- | --- | --- | --- | --- |
| TD-043 | The honesty sentence lives in the prompt file, not in code like the untrusted notice. A user prompt override can remove it. | M1.12 follow-up; ADR-056 | high: the honesty rule (golden rule 1, spec 3.1) should not depend on text an override can change | Before the first generation task (M6). Needs your approval, since it changes honesty wording. |
| TD-044 | Fake-provider output for real task types must carry provider and model `fake` and show a visible label. The rule is planned but not built. | ADR-088 Consequences; ADR-089 item 8 | high: fake drafts could be taken for real AI output | M3.4 and M3.5, in the plan. Must be in place before any real output is stored. |
| TD-045 | Lists of untrusted documents (several postings or web results) are not supported. They need a typed sequence with one wrapped block per item. | ADR-055 (Deferred) | medium: needed before a task reads several documents | With the first task that needs a list (M6 or M8). |
| TD-046 | The Claude provider refuses web-search requests until the usage hook is wired. | ADR-080 update (2026-10-11); ADR-057; ADR-076 item 12 | medium: web search is unavailable. Removing the refusal before the hook exists would let searches go uncounted | Before M8. Remove the refusal in the same change that wires the hook. |
| TD-047 | Heartbeats come only from handler steps. Nothing beats during a provider call, so a task type's timeout must cover its longest call. | Spec section 5 (M2 notes); M2.10 Evidence | medium: a long call can be marked interrupted by the watchdog | Before M3.4 sets its timeouts (`extract_cv` is a long Thorough call). Or add a heartbeat hook. |
| TD-048 | The SDK waits about 5 s after closing stdin before it stops a CLI that ignores it. A cancel of such a call takes about 5 s. A shorter cancel needs private SDK APIs. | M2.13 Evidence | low: slow cancel in a rare case | Re-check on SDK upgrades. |
| TD-049 | A rate-limit frame whose `rate_limit_info` is not a dict raises `TypeError`. The call then ends as `NetworkUnavailable`. | ADR-078 known gap | low: wrong error type; nothing leaks | When touched. |
| TD-050 | `FakeAIProvider.tool_calls` grows without bound in app mode, because a test pins that it is kept. | M2.16 Left (c) | low: memory in a testing-only path | When that test changes. |
| TD-051 | Heartbeat progress publishes no task event. Progress bars move on state changes, or on the panel's 2 s timer. | M2.6 notes; M2.20 (ADR-077) | low: progress looks stale between ticks | When the panel needs live progress. |
| TD-052 | Bursts of task events larger than a stream's queue end healthy streams, which then reconnect and resync. Coalescing is not built. | M2.18 Evidence (not done) | low: extra reconnects during bursts | When bursts are seen in use. |
| TD-053 | The result link on a task row is an empty placeholder. Practice tasks store `practice:5` as `result_ref`, and no results table exists until M3. | M2.19 notes; M2.17 Evidence | low: nothing to open yet | M3 results work. |
| TD-054 | The Claude provider reads the credential on the event loop before its first worker thread. ADR-063 says a separate task would track it, but none is recorded. (status unclear) | ADR-063 follow-up | low: a short block of the loop | Confirm whether a task exists. Otherwise fix when touched. |

## 6. Usage and budgets

| ID | Item | Origin | Impact | When / fix |
| --- | --- | --- | --- | --- |
| TD-055 | Actions that make several requests are checked only for the next request. A five-request task can be confirmed at "number 30 of 40" and then fail at the blocking point after one request. | M2.24 Decisions needed (2); ADR-086; ADR-089 item 10 | medium: a confirmed action can stop partway | M2 acceptance asks for a decision before M3. ADR-089 says it stays open until a task makes several calls. Decide with the user. |
| TD-056 | `UsageService.acknowledge` is no longer called by the app, but the method and its tests remain. | M2.24; ADR-086 item 1 | low: dead code with a looser rule | Remove when you approve. |
| TD-057 | `status`, `preflight` and `BudgetGate.blocked()` take the write lock although they only read. Page loads of app pages also read the task count. | ADR-079 Consequences; M2.24 Known limits; M2.20 notes | low: lock waits if the panel polls often | Revisit if the panel polls often. |
| TD-058 | The warning acknowledgement and the enqueue are two transactions. A failure between them can record a warning as shown with nothing queued. | M2.24 Known limits | low: that warning is not shown again in the cycle | When the gate is next changed. |
| TD-059 | "Remove budget" has no undo. It clears the budget at once. | ADR-083; M2.22 | low: a mistaken remove means typing the budget again | Undo is an open UX decision. |
| TD-060 | "Background tasks at once" is stored now and applies only at the next start. | ADR-082 | low: no effect until restart | Needs a runner change and a new decision. |
| TD-061 | On the web-search step, continuing with empty fields while stored budgets are unreadable marks the step done, while AI actions stay paused. | M2.25 Known limits | low: the step says done while budgets are unreadable | Fix in the step handler when touched. |
| TD-062 | `TierCapReached.user_message` always offers Fast. The gate's copy does not when Fast is not free. | M2.27 Left (2) | low: inconsistent offer | When the copy is reviewed. |
| TD-063 | Budget copy is not approved: the Settings section, the usage panel and the warning dialog. Spec 2.11 says "Monthly limit reached" where the panel says "Blocking point reached", and "Thorough limit" where the forms say "cap". | M2.22 to M2.24 Copy to review; M2.27 | low: wording may disagree with the panel | User review, then one change to spec 2.11 and the copy. |
| TD-064 | Web-search budget questions are open: whether web search has tiers at all; a "who pays" sentence (needs the provider's billing confirmed); deriving warnings and the blocking point from one number; showing the budget on Done. | M2.25 Left (3) | low: product and copy decisions | Decide with the user before M8. |
| TD-065 | Usage panel display issues: the breakdown's sideways scroll resets on refresh; a month count from an earlier cycle without a reset time stays in the list; a steady event stream delays the refresh until one quiet second. | M2.23 Known limits | low: small display issues | When the panel is next changed. |
| TD-066 | Testing tools does not refresh when a practice run reaches the blocking point. A refresh drops the "Practice task started." line. "Change limits" closes the dialog during an in-flight Continue. | M2.24 Known limits | low: stale panel until a reload | When the panel is next changed. |

## 7. Setup guide and UI

| ID | Item | Origin | Impact | When / fix |
| --- | --- | --- | --- | --- |
| TD-067 | A guide form error has both `role="alert"` and focus, so a screen reader may read it twice. | M1.22 Left; M1 acceptance notes | medium: accessibility | Fix with the next change to the guide forms. |
| TD-068 | The app has no persistent "Demo" marker in the shell or the database. | M2.26 Left (3) | medium: a user could mistake demo data for real data | Decide with the user. A marker in the shell is small. |
| TD-069 | A tier save swaps the whole Providers section, so a key typed but not saved is cleared. | M1.22 Left | low: typed text lost | When the Providers section is next changed. |
| TD-070 | Model options do not mark the provider's default model. | M1.22 Left | low: the default is not visible | Decide the copy (a "(default)" suffix was considered). |
| TD-071 | The Continue link disables the provider form's buttons while it loads. | M1.24 Left | low: brief disabled state | When touched. |
| TD-072 | A missing network shows as a wait of up to 60 s before "No connection". | M1 acceptance notes; ADR-058 timeout | low: slow feedback | Shorter feedback or a status line. |
| TD-073 | The provider-broken banner wording has not been looked at in the demo. | M1.20 Left | low: copy | User review. |
| TD-074 | Settings > Job preferences, when built, must use optimistic concurrency and show a conflict. The guide uses last-write-wins. | ADR-064 | low: a future screen could overwrite edits | When Settings > Job preferences is built. |
| TD-075 | Done step ideas deferred: an error summary with a count when several fields are refused; a different heading or disabled buttons on Done while required steps are unfinished. | ADR-064 (Deferred) | low: UX | Review with the UX reviewer. |
| TD-076 | Step 3 summary says "Set a monthly budget" even when one is stored. "Skip for now" and "Continue" with no budget behave the same apart from the Done label. | M2.25 Known limits; session log 2026-10-11 | low: copy | With the step 3 copy review. |
| TD-077 | Without scripts, form posts are refused (the launch token travels in htmx headers). Step 3 and the budget forms give no outcome message on a plain post. | ADR-060; M2.22 UX ideas; M2.25 Known limits | low: no-script users get no feedback | Decide whether a no-script path is in scope. |
| TD-078 | `test_provider_connection` still renders on the event loop. | M2.25 Known limits | low: may block the loop during a render | When touched. |
| TD-079 | A stale Testing tools form gets the shell's generic "Something went wrong" banner on its 404, because htmx does not swap 404s. | M2.17 Left (a); M2.19 notes | low: confusing message in a rare case | Changing it touches shared shell JS. |
| TD-080 | Demo wording is not final: "Fake (testing only)" or "Practice provider" (existing tests pin one); "1 task needs attention"; `MSG_SAVED_FAKE` still says "for testing only"; "This can take up to a minute." shows while the fake is tested. | M2.26 Left (2); M2 acceptance copy 4 | low: copy | Decide the wording. The tests change with it, so approval is needed. |
| TD-081 | "Looked at" for failed tasks is kept per browser origin. Another port or browser counts old failures again. | ADR-088 item 4; M2.26 Known limits | low: repeated "needs attention" | Accept, or move the flag to the database. |
| TD-082 | The announce-once rule for finished tasks does not check which window has focus. | M2.20 Known limits | low: a background tab may announce | When touched. |
| TD-083 | Starting a practice task in Settings > Testing tools still shifts the page by 36 px. Only the test was fixed. | M2.22 Known limits | low: layout jump | Fix the layout. |
| TD-084 | The Testing tools hint says "runs a few steps". It should say "makes 5 requests". The change alters an M2.17 test string, so it needs your approval. | M2.24 Decisions needed (3) | low: copy accuracy | With your approval. |
| TD-085 | Desktop sidebar collapse, sticky sidebar and density toggle are not built. (status unclear: recorded at M0, not re-checked) | M0 acceptance follow-ups; ADR-042 | low: UI polish | Decide in M9 polish. |
| TD-086 | Budget UX ideas not built: collapsing each service's form (34 tab stops on Settings), moving the usage section, thousands separators, threshold marks on the meter, `<meter>` instead of `<progress>`. The user asked for less attention to budgets, so these are low priority. | M2.22; M2.23 | low: UX ideas | Decide in M9 polish, or on request. |
| TD-087 | The Anthropic terms link on the provider step points to a URL that is in the code, but no approval of the URL or the wording is recorded. (status unclear) | ADR-044 item 14; M1.17 | low: an unapproved link and copy | Confirm the URL and wording with the user. |

## 8. Tests and CI

| ID | Item | Origin | Impact | When / fix |
| --- | --- | --- | --- | --- |
| TD-088 | `tests/unit/test_pytest_setup.py` may never run its copied conftest, because of importlib conftest reuse. Some pytester checks could be inert. (status unclear: a suspicion, not confirmed) | M2.3 Evidence | medium: some tests may pass without running | Candidate clean-up. Confirm first. |
| TD-089 | `test_ac_m2_5_complete_saves_result_and_success_in_one_transaction` could fail under load (an in-process race). M2.27 says the race was fixed in 17b42d5 and 5816038. The M2 acceptance report says it is not done. (status unclear) | M2.21 Left (2); M2 acceptance Known limits | low: a flaky test, and the records conflict | Confirm the fix, then correct the acceptance report. |
| TD-090 | `test_parse_killed_at_deadline` allows 2.0 s for the child to start. It may flake on a loaded machine. Raising it changes a test, so it needs your approval. | M3.1 Left (c) | low: a flaky run | Raise with your approval. |
| TD-091 | The `-m live` gate covers only `tests/live/`. Extending it would change `test_live_tests_run_with_env`. | M1.25 Left; M1 acceptance notes | low: a live test outside the folder could run unexpectedly | Needs your approval. |
| TD-092 | Runner branch coverage varies with timing (98.8% to 97.5%, lines 367 to 369) against the 95% gate. | M2.24 Known limits | low: the gate could flap | Check those lines when the runner changes. |
| TD-093 | Child-only code paths in `app/modules/extract/runner.py` are not seen by coverage. | M3.1 Evidence | low: hidden untested code; matters if the module joins critical coverage | When the module joins critical coverage. |
| TD-094 | `app/services/usage_values.py`, `app/services/usage_meter.py` and `app/modules/ai/metering.py` are not in the critical-coverage registry. They measure 97 to 100%. | M2.15; M2.16; M2.27 Left (2) | low: no gate on them | Registry change needs your decision. |
| TD-095 | `create_app` builds task ids with `UuidIdFactory` and has no `ids` parameter, so tests cannot inject task ids. | M2.8; `app/main.py` | low: tests are less deterministic | When the next task test needs fixed ids. |
| TD-096 | Tests reach private names: `_chosen`, `_cpu_limit_seconds`, `_MAX_ANSWER_BYTES`. | M3.1 Evidence | low: refactors break tests | When touched. |
| TD-097 | `tests/` is not type-checked. `uv run mypy` covers `app` and `scripts` only. | M2.3 Notes | low: test typing is unchecked | Candidate clean-up. |
| TD-098 | The no-automatic-retry test uses a sentinel task instead of a wait. A retry that fires later than the sentinel is not caught. | M2.21 Deviation | low: a delayed retry could pass the test | Accept, or add a timed check with an injected clock. |
| TD-099 | The demo harness's folder redirection is checked on macOS only. | M2.26 Known limits | low: the Linux path is untested | When the demo harness changes. |
| TD-100 | `test_fact_cannot_be_merged_into_itself` no longer isolates its check. A sibling test covers it. | M3.2 Left | low: a test that looks like it covers more than it does | M3.3 test cleanup. |
| TD-101 | The full keyboard walk runs at desktop only. The 360 px walk is not run; only the banner path is. | M1.24 Left; M1 acceptance notes | low: keyboard coverage at phone width | When touched. |
| TD-102 | Ubuntu runs the three smoke tests twice (smoke job and coverage job). | M0 acceptance | low: CI time | Remove the duplicate in a CI change. |
| TD-103 | `SdkRecordFilter` is added to pytest's shared caplog handler and is not removed at teardown. | M1.23 Left | low: test-order effects | When touched. |
| TD-104 | The real-subprocess fake-CLI stderr variant is not covered. A Windows-only skip would be needed, which needs your approval. The stderr path is covered through the replay transport. | M1.23 Left | low: a narrow gap | Needs your approval. Likely stays while Windows is suspended. |
| TD-105 | Test changes await your confirmation: the M2.18 paused clocks and 120 s checks in `test_events_reader.py`; the M2.23 to M2.25 changes (the Continue press in the warning test, the `finish` helper, the step 3 expectations). Two M2.23 e2e tests were written after the code they check. | M2.18; M2.23 to M2.25 Evidence; M2 acceptance Test changes | low: tests may not prove what they seem to | Confirm each change with the user. |

## 9. Tooling and docs

| ID | Item | Origin | Impact | When / fix |
| --- | --- | --- | --- | --- |
| TD-106 | Spec 2.11 has no rows for the seven document and upload errors: `UnsupportedDocumentType`, `UnreadableDocument`, `EncryptedDocument`, `NoTextFound`, `ParseTimedOut`, `UploadTooLarge`, `UploadNotAccepted`. | ADR-089 update (M3.1) | medium: spec and code disagree on error copy | M3.10. |
| TD-107 | Spec 2.3, 2.12, 2.13, 4.3 and section 5 wait for their M3 updates. | ADR-089 Consequences | medium: the spec describes the code less well each milestone | M3.10. |
| TD-108 | The `add-ai-task` skill does not show the ADR-080 `run_task` and `meter` signature. The `add-provider` skill (line 14) says "tokens when reported", but token budgets are deferred. | M2.27 Left (3) | medium: agents follow an out-of-date recipe | Before the next AI task. |
| TD-109 | Spec 4.1 is not updated for the mixed-session fix and the failures package. It needs your approval under the spec rule. | M2.3 Evidence; M2 acceptance item 11 | low: test documentation is out of date | Your approval. |
| TD-110 | `pypdf` and `python-multipart` have lower bounds only in the dependency declaration. | M3.1 Evidence | low: a future release could break unseen | At the next dependency review. Can share the audit in TD-001. |
| TD-111 | `httpx` with `starlette.testclient` triggers a Starlette deprecation warning in every test run. Switching adds a dev dependency. | Spec 7.5 | low: noise now; breakage on a Starlette upgrade | Before M9 release hardening, or on a Starlette upgrade. |
| TD-112 | AC-M2.10 says "provider" where ADR-076 item 4 says "service". | M2.27 Left (2) | low: wording | Next spec pass. |
| TD-113 | Stale pointers: ADR-064 has no pointer to ADR-087. ADR-041 names `app/render.py`, which ADR-044 corrects to `app/api/templating.render`. | M2.27 Left (2); ADR-044 docs note | low: misleading references | Add the pointers in a new ADR or the next docs pass. Old ADRs are not rewritten. |
| TD-114 | `m1_registry` was not renamed. Fourteen test modules import it. | ADR-087 item 4 | low: the name does not match its content | When the next registry test changes. |

## 10. Platform

| ID | Item | Origin | Impact | When / fix |
| --- | --- | --- | --- | --- |
| TD-115 | Windows is not supported (ADR-061). Windows-specific code, tests and notes remain, and nothing runs or maintains them. Recorded Windows limits are unchecked: the `claude.exe` lookup, Credential Manager's value limit (about 1,280 characters), `os.replace` while another program holds `config.toml`, and the data-folder lock, which is a no-op there. | ADR-061; ADR-046; ADR-048; ADR-043; ADR-040 | medium: dead code can rot without a signal | Remove it at M9 release hardening, or resume with a new ADR. |
| TD-116 | Where `flock` fails (some network drives), the data folder is refused as unavailable, and the CLI prints a generic message. A "local folder only" note is not decided. | M2.4 Left; ADR-076 item 9 | low: a confusing message on network drives | Your decision on the note or the copy. |
| TD-117 | The owner check covers the data folder only, not its parent folders. A writable parent could swap the folder after the check. | ADR-043 (Not covered) | low: not a realistic threat under the home folder | Accept, or revisit if `--data-dir` points into shared places. |
| TD-118 | `prompts/` is not packaged into the wheel. A built wheel would not include the prompts the app loads. | ADR-044 item 13; M2.17 Left (b) | medium: a released package does not run its prompts | M9: add the packaging task and a wheel test. |
| TD-119 | macOS CI runs only on pull requests, manual dispatch and the weekly schedule. Pushes run Ubuntu only. | ADR-073; ADR-032 | low: a macOS regression can wait for a pull request | Accept, or change the CI policy with your approval. |

## 11. Unverified checks (live and manual)

| ID | Item | Origin | Impact | When / fix |
| --- | --- | --- | --- | --- |
| TD-120 | The AC-M1.9 live tests have not run. They need your own subscription token and optionally an API key. M1 was accepted without them. | M1.25; M1 acceptance; `docs/live-check-m1.md` section 1 | high: the real Claude sign-in path is unproven | You run section 1 of the live check and report the results. |
| TD-121 | ADR-045 questions are unanswered: `MAX_TURNS = 2` is enough; `--tools ""` with `dontAsk` still allows `StructuredOutput`; blanked values count as unset; which credential wins in token mode when a keychain login exists; managed settings and their hooks; the replay fixtures match the real frames. | ADR-045 follow-ups; live check section 2 | high: the isolation and mode choices rest on offline reading | Run section 2 of the live check and record each answer with a date. |
| TD-122 | Unverified: the keychain item used per mode, the empty config folder on first start, and the files written per mode under `~/.claude*`. | Live check sections 1, 3 and 4; ADR-057 | medium: isolation claims are unproven | Run sections 1, 3 and 4 of the live check. |
| TD-123 | Claude usage frames are unverified: whether a short call emits a frame at all; token versus existing sign-in; whether `utilization` is present when the status is allowed; where the frame sits relative to the result; real `resetsAt` values; whether `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` suppresses frames. The `unifiedWindows` block is not read. | ADR-078 checklist; M2 acceptance | medium: plan utilization is information only, so budgets do not depend on it | Run with the live check (M1.25). |
| TD-124 | The M2 manual checklist is unticked: budget screens in light and dark at 390 and 360 px; the tasks panel; crash recovery with kill -9; the step 3 walk; demo from scratch. You accepted M2 without ticking it. | `docs/acceptance/M2.md` Manual checklist | medium: visual and UX checks are not recorded | You walk the checklist and record the results. |
| TD-125 | The M1 manual checklist is unticked: the spec exit check with your own token (and an API key if you have one), and a visual check of Settings > Providers and the reopened guide in light, dark and phone width. | `docs/acceptance/M1.md` Manual checklist | medium: the exit check is not recorded | You walk it. |
| TD-126 | The M2.22 security review ran one test from outside the repository, without `tests/conftest.py`: no in-memory keyring and no credential scrubbing. The app may have read the real keychain entry or credential variables. Nothing was stored or printed. | M2.22 Evidence; M2 acceptance | medium: a possible read of real credentials, not recorded as settled | Decide whether to repeat the run from inside the repository. |

## Checked and closed (not listed)

So that nobody re-opens them by mistake:

- The M1 `asyncio.run` failure in mixed pytest sessions: fixed by M2.3.
- The SQLite INTEGER bound for usage counts: `SQLITE_INTEGER_MAX` in `app/domain/usage.py` (ADR-079).
- The data-folder lock: built in M2.4 (ADR-072 was the deferral).
- The Windows CLI question (ADR-045 update, 2026-10-04) and the Windows CI check (M0.19). Windows is now suspended (ADR-061).
- The macOS failure of `test_settings_tier_mapping_keyboard`: fixed in M2.1.
- The M2.20 top-bar e2e test: stabilised by `assert_one_row` (commit c8c5a42).
- The stream cap and the opt-in `data-events-url`: built in M2.18.
- The "Keychain couldn't be read" copy for the environment fallback: built in M1.17.
