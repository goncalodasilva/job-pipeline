# Job Search Pipeline — Technical Specification

Version 1.0 · 2026-10-03 · Audience: the maintainer and the Claude Code agents that build this app

This is the implementation contract. It turns the product and design documents into architecture, interfaces, rules, a milestone roadmap with test plans, and a description of the agentic system used to build the app. The keywords MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## Contents

0. How to use this document
1. System overview
2. Architecture
3. Cross-cutting implementation rules
4. Testing strategy
5. Roadmap and milestone test plans
6. The agentic development system
7. Appendices: commands, test markers, environment variables, glossary

---

## 0. How to use this document

### 0.1 Sources and precedence

| Priority | Source | Holds |
| --- | --- | --- |
| 1 | `docs/DECISIONS.md` | Recorded decisions (newest entry wins) |
| 2 | `docs/TECHNICAL_SPEC.md` (this file) | Architecture, contracts, rules, roadmap, test plans |
| 3 | `docs/references/detailed-design.md` | Screens, interactions, UX rules, data model draft |
| 4 | `docs/references/product-spec.md` | Original product vision |

When two sources disagree, agents MUST stop and ask the user, then record the answer in `docs/DECISIONS.md`. Agents MUST NOT silently pick one.

### 0.2 Where to look

| You need | Read |
| --- | --- |
| What to build next | `docs/PROGRESS.md`, then section 5 for that milestone |
| How modules may depend on each other | 2.3 |
| Interfaces for AI, search, calendar, secrets, storage | 2.4 |
| Security rules | 2.9 and 3.3 |
| The honesty rule and how it is enforced | 3.1 |
| How to test and what "done" means | 4 |
| How the agents, skills and hooks work together | 6 |
| What a screen looks like and how it behaves | `docs/references/detailed-design.md` |

---

## 1. System overview

### 1.1 Purpose

A local, open-source web app that helps one person run a job search: it collects job postings, summarizes and assesses them, helps the user position themselves for each role with a growing bank of personal facts, drafts tailored CVs and cover letters, tracks applications on a board, and prepares the user for interviews. The user always submits applications personally.

### 1.2 Engineering principles and how they are enforced

| Principle | Enforcement |
| --- | --- |
| Human in the loop | The app never submits applications or automates employer sites. AI output is always a draft until the user approves it. |
| Honesty first | Generated text must cite the confirmed facts it uses; uncited or unsupported content is flagged and blocks approval (3.1). |
| Local-first and private | Server binds to 127.0.0.1 only; data lives in a data folder outside the repository; secrets live in the OS keychain (2.9). |
| Swappable providers | AI, search, calendar, secrets and storage sit behind Python protocols with capability flags and shared contract tests (2.4, 4.1). |
| On demand | AI and web search run only from explicit user actions, as visible, cancellable background tasks, under optional usage budgets (2.5, M2). |
| Reusable by anyone | No personal data, criteria or credentials in code; everything is configurable; a fictional demo dataset ships in `demo/`. |
| Testable | Clock, id generation and providers are injected; tests never touch the network or real providers unless explicitly marked live (4). |

### 1.3 Non-goals

Automatic applying; scraping sites whose terms forbid it; a hosted or multi-user service; native mobile apps; real-time collaboration; background processes that run while the app is closed.

### 1.4 Quality targets

| Attribute | Target | How it is checked |
| --- | --- | --- |
| Responsiveness | Page and fragment requests p95 under 150 ms server time with 1,000 jobs, excluding AI tasks | Performance test in M4 and M9 |
| No lost work | Every user edit autosaves; every task ends in a terminal state | Integration and failure-injection tests (M2) |
| Privacy | No secret ever appears in logs, responses, exports or the database | Automated scans in tests (M0, M1) |
| Accessibility | WCAG 2.2 AA on core flows | axe-core checks in e2e tests, plus a manual keyboard and screen-reader pass (M9) |
| Portability | macOS, Linux; Python 3.11 and newer (Windows suspended, ADR-061) | CI matrix (4.6) |

---

## 2. Architecture

### 2.1 Context

```mermaid
flowchart LR
  User((User)) --> Browser[Browser]
  Browser -->|HTTP, 127.0.0.1 only| App[Job Search Pipeline<br/>local Python app]
  App --> Data[(Data folder:<br/>SQLite, documents, logs)]
  App --> Keychain[OS keychain]
  App -->|Agent SDK or API key| Claude[Claude]
  Claude -->|built-in web search,<br/>only when requested| Web[Web]
  App -->|user-supplied job URLs| JobPages[Job pages]
  App -->|.ics files| Calendar[User's calendar app]
```

Everything runs on the user's computer. The only outbound traffic is: calls to the configured AI provider, web search performed by that provider when the user asks for it, and fetching a job page URL the user pasted.

### 2.2 Components

```mermaid
flowchart TD
  subgraph Presentation
    API[app/api: pages, htmx fragments, JSON]
    TPL[app/templates + app/static]
  end
  subgraph Application
    ONB[app/onboarding: setup guide]
    SVC[app/services]
    TR[app/tasks: task runner]
  end
  subgraph Domain
    DOM[app/domain: models, state machines,<br/>honesty checker, budgets math]
  end
  subgraph Integration
    AI[modules/ai]
    SE[modules/search]
    CA[modules/calendar]
    ST[modules/storage]
    SEC[modules/secrets]
    FE[modules/fetch]
    RE[app/render]
  end
  API --> TPL
  API --> ONB
  API --> SVC
  API --> TR
  ONB --> SVC
  SVC --> DOM
  SVC --> AI
  SVC --> SE
  SVC --> CA
  SVC --> ST
  SVC --> SEC
  SVC --> FE
  SVC --> RE
  TR --> SVC
```

### 2.3 Layering and dependency rules

These rules are enforced by import-linter contracts in `pyproject.toml` and checked by `uv run lint-imports` on every verification run.

1. `app.domain` MUST be pure Python: no I/O, no framework imports, nothing from other `app` packages.
2. `app.modules.*` MAY import `app.domain`. They MUST NOT import `app.services`, `app.api`, `app.onboarding` or `app.tasks`.
3. Vendor SDKs MUST be imported only inside the provider package that wraps them. For example, `claude_agent_sdk` is imported only in `app.modules.ai.providers.claude_sdk`, and `keyring` only in `app.modules.secrets.providers.keyring_store`.
4. `app.services` MAY import `app.domain` and the public interface of each module (`app.modules.<name>`, not its `providers` subpackage). Services MUST NOT import FastAPI, Starlette or Jinja2.
5. `app.api` MAY import services, onboarding and the task runner's public functions. It MUST NOT import `app.modules.*` or `sqlite3`.
6. `app.tasks` MAY import services and domain. Task handlers are thin: they call a service function. The runner reaches the AI module and storage only through services; the 2.5 diagram shows the logical flow (ADR-034).
7. Provider selection happens in one place, `app/wiring.py`, which builds concrete providers from settings and hands them to services through constructor injection.

### 2.4 Key interfaces

Signatures are abbreviated. Implementations MUST keep these shapes unless an ADR changes them. The code versions, with fetching behind an injected `JobPageFetcher`, are recorded in ADR-036.

```python
# app/modules/ai/__init__.py
class AIProvider(Protocol):
    name: str
    capabilities: AICapabilities   # web_search, speech_to_text, text_to_speech,
                                   # reports_token_usage
    async def test_connection(self, model: str) -> ConnectionResult: ...
    def known_models(self) -> list[ModelChoice]: ...
    async def run(self, request: AIRequest, cancel: CancelToken) -> AIResponse: ...

@dataclass(frozen=True)
class AIRequest:
    task: str                      # e.g. "tailor_cv"
    prompt_id: str
    prompt_version: str            # e.g. "3", or "3+override" (ADR-055)
    system: str
    user: str                      # rendered prompt; untrusted content already delimited (3.3)
    output_schema: dict            # JSON Schema the response must satisfy
    model: str                     # resolved from the task's tier (Fast or Thorough)
    allow_web_search: bool = False
    on_tool_use: Callable[[str], Awaitable[bool]] | None = None   # budget hook; False blocks
    on_usage: Callable[[ProviderUsageSnapshot], None] | None = None   # provider-reported usage, during the call (ADR-078)

@dataclass(frozen=True)
class AIResponse:
    raw_text: str
    data: dict                     # parsed JSON object; schema validated by the task executor (ADR-054)
    model: str
    usage: TokenUsage | None       # when the provider reports it

# Task-level functions live in app/modules/ai/tasks.py, one per AI task (see 3.6),
# e.g. async def summarize_job(ctx: AITaskContext, job_text: str, prefs: Preferences) -> JobSummary
```

Added in M2 (ADR-078, ADR-080). `ProviderUsageSnapshot` (service, window, unit, used, limit, utilization, resets_at, observed_at) is validated when built. `UsageMeter` is implemented by the usage service (`ServiceUsageMeter`, `app/services/usage_meter.py`), never by a module. `run_task` requires `meter`, so a provider call can't run uncounted.

```python
# app/modules/ai/__init__.py (ADR-080)
class UsageMeter(Protocol):
    def reserve(self, *, tier: str, task_type: str, task_id: str | None, kind: UsageKind) -> str: ...
    def complete(self, record_id: str, *, success: bool, tokens: TokenUsage | None) -> None: ...
    def snapshot(self, snapshot: ProviderUsageSnapshot) -> None: ...

class AITaskRunner(Protocol):
    async def run_task(self, task: str, inputs: Mapping[str, object], cancel: CancelToken, *,
                       meter: UsageMeter, tier: Tier | None = None, task_id: str | None = None) -> TaskResult: ...
```

```python
# app/modules/search/__init__.py
class SearchProvider(Protocol):
    name: str
    mode: Literal["delegated", "standalone"]   # delegated: the AI provider searches itself
    storage_allowed: bool | None               # None means unclear: do not cache derived output
    reports_usage: bool
    async def search(self, query: str, limit: int) -> list[SearchResult]: ...   # standalone only

# app/modules/calendar/__init__.py
class CalendarProvider(Protocol):
    name: str
    supports_sync: bool
    def export_event(self, event: EventData) -> CalendarFile: ...               # .ics bytes
    async def sync_event(self, event: EventData) -> ExternalRef: ...           # later providers

# app/modules/secrets/__init__.py
class SecretStore(Protocol):
    available: bool
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...
# Implementations: KeyringSecretStore, EnvSecretStore (read-only fallback), MemorySecretStore (tests).

# app/modules/storage/__init__.py
class Database:
    def transaction(self) -> ContextManager[Connection]: ...  # BEGIN IMMEDIATE ... COMMIT
# Repositories are plain functions or small classes per aggregate (jobs, facts, applications...).

# app/modules/fetch/__init__.py
async def fetch_job_page(url: str, limits: FetchLimits) -> FetchedPage   # SSRF-guarded (2.9)

# app/domain/clock.py and ids.py
class Clock(Protocol):
    def now(self) -> datetime: ...   # always UTC
class IdFactory(Protocol):
    def new(self) -> str: ...
```

### 2.5 Runtime flows

Page and fragment requests: route → service → repository → template. Full pages render the app shell; htmx requests (`HX-Request: true`) receive fragments only.

AI actions:

```mermaid
sequenceDiagram
  participant B as Browser (htmx)
  participant R as Route
  participant S as Service
  participant T as Task runner
  participant U as Usage budget
  participant A as AI module
  participant P as Provider
  B->>R: POST action (launch token and origin)
  R->>S: validate input
  R->>U: budget check before enqueue (BudgetGate, ADR-086)
  U-->>R: start, warning to confirm, or blocked
  R->>S: enqueue(type, target) when started (dedupe key)
  S-->>R: task id (an active duplicate's id if one exists)
  R-->>B: fragment with task status
  T->>A: task function (prompt, schema, tier), once claimed
  A->>U: reserve usage record before each provider call (ADR-080)
  U-->>A: record, or BudgetBlocked
  A->>P: run(request)
  P-->>A: response
  A-->>T: validated data
  A->>U: complete usage record
  T->>S: save draft + mark task succeeded (one transaction)
  T-->>B: event stream "task-updated" (id, type, status)
  B->>R: refetch fragments on tasks:updated
```

The budget check runs in the route, before anything is queued, so a start that is blocked or waits for a confirmation queues nothing (ADR-086). The executor's reserve is the second check: it counts each provider call before the call and refuses it at the blocking point (ADR-080). Budgets are per service and count requests only (ADR-076 items 4 and 5); the rules are in 3.7.

### 2.6 Process model

One Python process runs Uvicorn with the FastAPI app. The task runner is an asyncio worker started in the app's lifespan, with a semaphore limiting concurrent external calls (default 1, user-configurable up to 3). Each Agent SDK call runs the Claude Code CLI as a subprocess, which the SDK manages. Nothing runs when the app is closed.

### 2.7 Data storage

Data folder layout:

```
<data folder>/
  app.db                    SQLite database (WAL mode)
  backups/                  automatic copies before migrations (last 5 kept)
  documents/<application>/  exported CVs and letters, by version
  imports/                  uploaded CV files
  assets/                   user photo for CV templates, if any
  prompts/overrides/        user-edited prompt templates
  exports/                  JSON exports
  logs/                     rotating logs (redacted)
```

SQLite settings: `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`. Transactions are short and use `BEGIN IMMEDIATE` for writes.

Migrations are numbered SQL files in `app/modules/storage/migrations/NNNN_description.sql`, applied in order, each in a transaction, and recorded in `schema_migrations`. They are forward-only. Before applying pending migrations to an existing database, the app copies `app.db` to `backups/`. A test MUST prove that a fresh database and a database upgraded step by step end with identical schemas. On POSIX systems, database files and backups are created with 0600 permissions and data folders with 0700 (ADR-038).

Full-text search uses FTS5 tables for facts and jobs, kept in sync by triggers.

The table list in `detailed-design.md` (State machines and database schema) is the starting point for the schema. Every mutable table carries `created_at`, `updated_at` and an integer `version` for optimistic concurrency.

### 2.8 Configuration

- **Bootstrap config:** `config.toml` in the platform config folder (`platformdirs.user_config_dir("jobpipeline")`), holding `data_dir` (absolute path; `~` is expanded) and the app version that created it. Its absence means first run (see M1). The file is always parsed, so malformed TOML is reported even with `--demo` or `--data-dir`.
- **Settings:** a `settings` table of JSON values, each validated by a typed model (providers, tiers, budgets, preferences, lists, statuses, appearance, session frequency).
- **Precedence:** CLI flag, then environment variable, then bootstrap config, then defaults (port 8000, log level INFO). See 7.3 for environment variables and defaults.
- **Environment variables:** listed in 7.3. `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` are fallbacks only, for systems without a keychain and for development.

### 2.9 Security model

1. The server MUST bind to 127.0.0.1. Requests whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>` are rejected (defeats DNS rebinding).
2. A random launch token (32 bytes, URL-safe) is generated at startup and embedded in the base template. htmx sends it on every request as `X-JP-Token` via `hx-headers`. Every fragment, JSON and state-changing endpoint MUST require it. Full-page GETs, static files and `GET /health` MAY omit it (ADR-023).
3. State-changing requests MUST also carry an `Origin` or `Referer` matching the app's own origin.
4. Jinja2 autoescaping stays on. AI output and job text MUST NOT be marked safe. Where Markdown from AI is rendered, it is converted and then sanitized with nh3.
5. Uploads: 10 MB limit, type checked by content (PDF, DOCX, plain text), stored under the data folder with generated names, parsed with timeouts.
6. Job URL fetching: http and https only; the resolved address MUST NOT be private, loopback, link-local or multicast, checked again after each redirect; 5 MB and 15 s limits; no cookies. This module never crawls: one user-supplied URL at a time.
7. Untrusted content (job text, web results, CV text) is delimited and treated as data (3.3). Plain AI tasks run with no tools allowed; only explicitly requested web questions allow the provider's web search tool.
8. When the app calls the Agent SDK, it MUST isolate the call from any developer configuration: run in an empty temporary working directory and disable loading of filesystem settings (the SDK's `setting_sources` option at the time of writing; verify against the installed SDK version).
9. Secrets live only in the keychain (or the environment fallback). They MUST NOT be stored in the database, written to logs, returned by any endpoint, or included in exports. A logging filter redacts configured secret values and token-shaped strings.
10. The app sends no telemetry.
11. Every HTTP response, rejections included, carries `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`, so no other site can frame the app and trick a click (ADR-039).
12. Only routes the server registers as pages (`register_pages`, `tags=[PAGE_TAG]`) and that dispatch actually picks may load without the token, and only without `HX-Request`; every other route needs it by default. GET and HEAD routes MUST NOT have side effects: no writes, tasks, provider calls, secret reads or exports, since they skip the origin check. Token-free GETs return only full HTML pages. The M2 routes aren't pages either, so each needs the token: `GET /events` (the live-update stream, `text/event-stream`, still side-effect free; ADR-077), `GET /tasks/panel`, `GET /tasks/indicator`, `GET /tasks/{id}`, `GET /settings/usage`, `GET /settings/testing-tools`, and the POST routes `/tasks/practice`, `/tasks/{id}/cancel`, `/tasks/{id}/retry`, `/settings/usage/budgets` and `/settings/usage/concurrency`, which also need the origin (rule 3). The browser's reader for `GET /events` sends the token as a header; there is no cookie and no token in a URL (ADR-076 item 1, ADR-077). Never add CORS headers, and never put the token in a URL or log (ADR-039).
13. The launch token stops other websites from acting through the browser; it is not authentication. Any local process or other user on the same computer can load a page and read it.
14. The data folder is locked while the app runs (ADR-072, ADR-076 item 9, M2.4). Before logging, the database, migrations or the AI temporary clean-up, the CLI and the app's start take `flock(LOCK_EX | LOCK_NB)` on `<data folder>/.lock`. The file is created with mode 0600 and refused unless it is a private regular file owned by the user with one link. A second copy gets `DataFolderInUse`, whose message names no path, and exits. The lock is POSIX only; where `fcntl` is missing it does nothing (ADR-061). The operating system releases it when the process exits or is killed.

### 2.10 Logging and diagnostics

Standard library logging with a JSON formatter, rotating files in `logs/` (5 files of 1 MiB; ADR-037). Prompt contents and personal data are not logged by default; a local debug setting can enable prompt logging and shows a warning while on. `GET /health` returns app version, database status and task runner status.

Task, service, AI-module and event-stream loggers write constant format strings. Their arguments are ids, task type names, counts and exception type names, never provider output, error text, prompts or row content. `tasks.error` stores the error kind only, and each event frame carries an id, a type and a status only (ADR-088, ADR-077). The leak scans of AC-M1.4 cover tasks, usage rows, events and logs (spec 5, M2).

### 2.11 Errors

| Error | Raised by | User-facing message pattern |
| --- | --- | --- |
| `ProviderNotInstalled` | AI provider | "Claude Code wasn't found. Install it, or choose Claude via API key." |
| `ProviderAuthError` | AI provider | "The token or key was rejected. Create a new one and paste it again." |
| `UsageLimitReached` | AI provider | "Your Claude plan's usage limit was reached. Try later or switch provider." |
| `ProviderRequestRejected` | AI provider | "Claude refused the request. Check the model in Settings > Providers." (unknown model, prompt too long, schema refused; ADR-057) |
| `BudgetBlocked` | Usage service | "Monthly limit reached for <service>. Resets on <date>." |
| `NetworkUnavailable` | Fetch, providers | "No connection. Nothing was lost; try again." |
| `InvalidModelOutput` | AI module | "The AI response didn't have the expected shape. Try again." (after one automatic retry) |
| `VersionConflict` | Repositories | "This item changed elsewhere. Reload to see the latest version." |
| `FetchRefused` | Fetch | "This address can't be fetched. Paste the job text instead." |
| `SecretStoreUnavailable` | Secrets (keychain store) | "Your system keychain isn't available. You can use an environment variable instead." (ADR-046) |
| `DataFolderInUse` | Workspace open | "This folder is already open in another copy of the app. Close that copy first, or choose another folder." (ADR-072, ADR-076 item 9) |
| `TaskNotRetryable` | Task service | "This task can't be retried. Only failed, cancelled or interrupted tasks can run again." (M2.7) |
| `TaskFailed` | Task runner | "This task stopped because of an unexpected problem. Try again." (an unexpected exception in a handler; M2.8) |
| `UnknownTaskType` | Task runner | "This task can't run in this version of the app." (M2.8) |
| `TierCapReached` | Usage service | "The Thorough limit for <service> is reached. It resets on <date>; you can run this once on Fast." (a `BudgetBlocked` subclass; the Fast sentence is offered only when Fast is free, otherwise the message ends after the date; ADR-079, M2.24) |
| `UsageLimitsUnreadable` | Usage service | "Your usage limits couldn't be read. Open Settings > Usage budgets and save them again." (ADR-079) |
| `BudgetNoticeRefused` | Usage service | "This warning can't be confirmed now. Reload the page to see your current usage." (ADR-079) |
| `PracticeNotOffered` | Practice service | "Practice tasks are only available while the fake provider is active." (M2.17) |
| `BUDGET_MESSAGE` (a constant, never raised: the copy for a stored `BudgetBlocked` kind) | Returned by `message_for_kind` (`app/domain/errors.py`), used by task views | "A monthly usage limit was reached. Open Settings > Usage budgets to see when it resets." (a stored kind keeps no service, tier or date; M2.19) |

Each error has a stable `kind` (a class attribute, by default the class name); tasks store the kind, never the message or detail (M2.7).

Refusals of a start answer 409, for htmx and for a plain request, with the reason: a blocking point, a tier cap without a free Fast tier, or unreadable budgets (ADR-086). A warning that isn't confirmed answers 200 with the warning dialog for htmx, and 409 as text for a plain request (ADR-086). A stale version of a budget form, or of the guide's budget step, answers 409 with the reload prompt (`fragments/shell/_reload_prompt.html`, `role="alert"`): the form's own explanation, then a "Reload the page" link. In the usage budget forms the message is `VersionConflict`'s copy (ADR-081, ADR-087).

`CallCancelled` (AI module) is not a user-facing error: a cancelled call ends quietly and the task shows as cancelled (ADR-054).

### 2.12 Technology stack

| Concern | Choice | Notes |
| --- | --- | --- |
| Language | Python 3.11+ | Type hints everywhere |
| Packaging | uv, `pyproject.toml`, `uv.lock` | Console script `jobpipeline` |
| Web | FastAPI on Uvicorn, Jinja2 templates | Server-rendered pages and fragments |
| Interactivity | htmx (vendored, pinned); live updates use a small fetch-based reader (no `EventSource`, no SSE extension; ADR-077); SortableJS (vendored) for board drag-and-drop; small vanilla scripts for shortcuts and the command palette | No frontend build step |
| Database | SQLite via the standard library `sqlite3`, thin repositories, SQL migrations | No ORM |
| AI | `claude-agent-sdk` (Python) | Behind the AI module |
| Secrets | `keyring` | Env fallback |
| Paths | `platformdirs` | Bootstrap config and default data folder |
| Job page fetch | `httpx` with the SSRF guard | Text extraction with a lightweight HTML parser |
| CV import | `pypdf` or `pdfplumber` for PDF, `python-docx` for Word | Chosen in M3 by fixture quality |
| Rendering | PDF from HTML (WeasyPrint is the default candidate), Word via `python-docx` | M6 starts with a spike: WeasyPrint needs system libraries on some platforms |
| HTML sanitizing | `nh3` | For rendered Markdown |
| Calendar | `icalendar` | .ics generation |
| Validation | `pydantic` v2 | Settings, AI schemas, DTOs |
| Tests | `pytest`, `pytest-asyncio`, `hypothesis`, `pytest-cov`, Playwright for Python, axe-core injected through Playwright | 4 |
| Quality | `ruff` (lint and format), `mypy`, `import-linter`, `pre-commit` with `gitleaks` | 4.5 |

**Live updates (ADR-077, ADR-088).** There is no htmx SSE extension and no `sse-connect`. Pages that show task state set `data-events-url` (`{% set live_updates = true %}`); the setup guide doesn't, so it opens no stream. The reader in `app/static/js/app.js` sends `X-JP-Token` as a header on every attempt and never puts the token in a URL.

The stream at `GET /events` (`text/event-stream`, `Cache-Control: no-store`) sends:

- `event: resync` first, on every connection;
- `event: task-updated` for each committed task change, with JSON `{"id", "type", "status"}` and no other field (a closed serializer);
- `: keepalive` comments about every 15 seconds while idle.

It ends when shutdown starts (the CLI sets the shutdown signal before uvicorn waits for open connections), and when its client stops reading (a bounded queue of 100 frames; the page then reconnects and resyncs). The reader reconnects with a backoff of 1, 2, 4, 8, 16 and then 30 seconds, and resyncs on every connect. A 403 shows the restart message and stops. A hidden tab closes its stream. Tabs of one browser share one stream through a Web Lock and a BroadcastChannel, both named `jobpipeline-task-events`; without them each visible tab reads its own.

The reader turns `resync` and `task-updated` into the body events `tasks:resync` and `tasks:updated`, both with no detail. Listeners refetch and never read a status from an event, because events can arrive out of order. The other two body events come from HTTP answers, not from the stream:

- `tasks:resync`: on every connect, and when a tab is shown again; the server also sends it in an `HX-Trigger` header after a cancel or retry;
- `tasks:updated`: for each task change;
- `usage:changed`: sent by a budget save or removal once the swap has settled (`HX-Trigger-After-Settle`);
- `tasks:finished`: every `GET /tasks/indicator` answer sends it in an `HX-Trigger` header, with the detail `{"tasks": [{"key", "message", "age"}]}` (the key is the task id and attempt, and age is whole seconds since the task finished). The page announces each once in the live region, keeps its key in localStorage (`jobpipeline-tasks-announced`, the newest 50), and on the first answer of a page announces only tasks that ended in the last 10 seconds.

Who refetches: the top-bar indicator and the tasks panel refetch on `tasks:resync` and `tasks:updated`; an open panel also asks every 2 seconds while it shows a task with a progress bar (queued or running), and once a minute when idle; each task row refetches on both events; the usage panel refetches on `usage:changed` and on `tasks:updated` (after a one-second delay); Testing tools refetches on `usage:changed`. A part that holds an open dialog doesn't refetch (ADR-086). A failed or interrupted task that the open panel hasn't shown yet counts on the closed Tasks button as "needs attention"; the keys seen are kept per browser origin in localStorage (`jobpipeline-tasks-seen`, ids and attempts only; ADR-088).

New runtime dependencies require the user's approval and an ADR.

### 2.13 Repository layout

```
job-pipeline/
  app/
    api/            routes: pages, fragments, JSON; middleware (host, token, origin)
    onboarding/     setup guide: detection, step registry, steps/
    services/       jobs, companies, applications, facts, sessions, tailoring,
                    prep, usage, settings, reminders, provider setup
    domain/         models, enums, state machines, honesty checker, budget math,
                    normalization, clock, ids, locales
    modules/
      ai/           interface, prompt library, executor.py, schemas, tasks.py,
                    providers/ (claude_sdk.py, fake.py)
      search/       interface, providers/
      calendar/     interface, providers/
      storage/      database.py, runner.py, migrations/, repositories
      secrets/      interface, providers/ (keyring_store.py, env.py, chained.py,
                    redacting.py, memory.py)
      fetch/        SSRF-guarded fetcher, HTML-to-text
    render/         CV and letter rendering (PDF, Word), country templates (planned, M6)
    tasks/          runner, handler registry, practice handler (the event publisher is api/events.py; ADR-077)
    templates/      Jinja2 pages and fragments
    static/         vendor/ (htmx and license only), js/, css/
    wiring.py       builds providers and services from settings (ADR-047, ADR-059)
    config.py       runtime config and data folder resolution (ADR-025, ADR-030)
    logging_config.py  logging setup with redaction (ADR-030, ADR-070)
    cli.py          jobpipeline command
    main.py         app factory and lifespan
  prompts/          default prompt templates and output schemas per task (3.6)
  demo/             fictional persona, facts and jobs (planned, M9)
  docs/
    PROGRESS.md     milestone tracking and session log
    DECISIONS.md    architecture decision records
    TECHNICAL_SPEC.md  this file
    live-check-m1.md  manual checklist for M1 live tests
    references/     product spec, detailed design
    acceptance/     acceptance reports per milestone
    development/    agent setup, handoffs and shared review notes
  scripts/          developer scripts (check_critical_coverage.py, etc.)
  tests/
    unit/           fast unit tests
    integration/    fast integration tests with real SQLite
    contract/       provider contract tests
    e2e/            end-to-end browser tests with vendor/axe-core (MPL-2.0)
    live/           live tests against real Claude services (opt-in, M1+)
    fakes/          scripting helpers, the SDK replay transport and keyring backends
                    (app-selectable fakes live in app/modules/*/providers/)
    fixtures/       ai/, prompts/, import_violations/, migration_hashes.json
    conftest.py     pytest configuration, keyring guard, session fixtures
    running_app.py  subprocess fixture (smoke and e2e)
    waiting.py      deadline-based waits (M2.3)
    asgi_stream.py  ASGI driver for the event stream (ADR-077)
    page_token.py   reads the launch token a page embeds (spec 2.9 rule 2)
    leak_scan.py    secret and personal-data leak scanner (M1.23, M2.26)
    setup_harness.py  completed setup helper
  .agents/skills/   shared procedures and native Codex workflows
  .claude/          native Claude agents, skill wrappers, rules, hooks, settings
  .codex/           native Codex agents, hooks, configuration and command rules
  AGENTS.md         shared coding-agent contract
  CLAUDE.md         imports AGENTS.md, adds Claude orchestration
  README.md         user guide and troubleshooting
  LICENSE           MIT license
  pyproject.toml    build config and dependencies
  uv.lock           locked dependency versions
  .env.example      sample environment variables (never commit with values)
  .gitignore        ignored files
  .gitattributes    vendored files marked -text (checksum invariant)
  .pre-commit-config.yaml  ruff format/check, gitleaks
```

---

## 3. Cross-cutting implementation rules

### 3.1 The honesty rule

The AI draws facts out of the user and rephrases them; it never invents experience. This is enforced in code, not only in prompts.

1. Generation tasks (`tailor_cv`, `draft_cover_letter`, `draft_narrative`, `adapt_answer`) receive only confirmed, current fact versions, each with an id such as `F12@3`, plus facts marked "never use in applications" removed and "generalize names" instructions applied.
2. Their output schemas require every content line or paragraph to carry a non-empty `fact_ids` list. Purely structural lines (headings, contact block) are typed `kind: "structure"` and need no ids.
3. After validation, `app/domain/honesty.py` checks every unit:
   - cited ids must exist in the input set, otherwise `unsupported`;
   - a content unit with no ids is `unsupported`;
   - numbers, percentages, years and money amounts that appear in the unit but in none of its cited facts are `needs_check`.
4. The UI shows these flags inline. Approval is blocked until each flagged unit is edited, removed, or confirmed by the user, and a user confirmation saves the claim as a new confirmed fact (the user is asserting it is true).
5. Feedback tasks (`answer_feedback`, `refine_fact`) may suggest wording but MUST NOT add claims; any claim not found in the fact bank is surfaced as "This isn't in your fact bank. Add it as a draft?".
6. Tests MUST include adversarial fake outputs (invented employer, wrong id, inflated number, uncited paragraph) and prove each is flagged.

### 3.2 Drafts, approval and versions

AI output is stored with status `draft` and becomes `approved` only by a user action. Every edit to a fact or document creates a new version. Marking an application "Applied" freezes the document versions and fact versions it used; later edits create new versions and never alter what was sent.

### 3.3 Untrusted content

Job postings, fetched pages, web search results, uploaded CV text and free-text user answers are untrusted input to prompts.

- They are inserted only through a template filter that wraps them:

  ```
  <untrusted_document source="job_posting" id="J42">
  ...content...
  </untrusted_document>
  ```

  Any occurrence of the closing tag inside the content is escaped.
- Every system prompt that includes untrusted content states that such content is data to analyze, never instructions. The renderer adds this notice itself, so templates and user overrides can't remove or reword it (ADR-055).
- Plain AI tasks run with no tools; web search is enabled only for explicitly requested web tasks.
- Model output is never executed, and never used to choose file paths or URLs to fetch automatically.

### 3.4 Determinism and testability

Services MUST NOT call `datetime.now()`, `uuid4()` or random functions directly; they use the injected `Clock` and `IdFactory`. Time is stored in UTC (ISO 8601); events also keep their IANA time zone name.

### 3.5 Concurrency

Mutable rows carry a `version` column. Updates include `WHERE version = ?`; a miss raises `VersionConflict`, shown to the user as a reload prompt. Background tasks follow the reliability rules in M2.

### 3.6 AI tasks and the prompt library

Each AI task has one prompt template, one output schema, a default tier, and fixture responses for the fake provider.

| Task | Default tier | Output, in brief | Milestone |
| --- | --- | --- | --- |
| `extract_cv` | Thorough | Draft roles, facts, stories, skills | M3 |
| `refine_fact` | Fast | Sharpening questions, suggested wording, no new claims | M3 |
| `summarize_job` | Fast | Summary, structured fields, requirements | M4 |
| `assess_fit` | Fast | Band, matched requirements with fact ids, missing, deal-breaker flags | M4 |
| `gap_check` | Thorough | Requirement-to-fact map, gaps, honest framing, worth-applying note | M6 |
| `propose_angles` | Thorough | 2–3 positioning angles with rationale | M6 |
| `tailor_cv` | Thorough | CV units with fact ids | M6 |
| `draft_cover_letter` | Thorough | Paragraphs with fact ids | M6 |
| `draft_cv_template` | Thorough | Country template specification | M6 |
| `enrichment_question` | Fast | One open question and why it is asked | M7 |
| `structure_answer` | Fast | Draft fact or story, missing parts | M7 |
| `interview_questions` | Thorough | Questions with type and origin | M8 |
| `answer_feedback` | Thorough | Checklist results, unsupported claims, suggested facts | M8 |
| `draft_narrative` | Thorough | Narrative card with fact ids | M8 |
| `adapt_answer` | Thorough | Adapted answer with fact ids | M8 |
| `company_brief` | Thorough | Brief with sources (uses web search) | M8 |
| `group_removal_reasons` | Fast | Themes and suggested preference changes | M9 |

Prompt files live in `prompts/<task>/` as `prompt.md` (YAML front matter plus template) and `schema.json`. The format is specified in `.agents/skills/add-ai-task/references/prompt-file-format.md`. Rules:

- Any change to a prompt's wording or schema bumps its `version`.
- Every generated record stores `prompt_id`, `prompt_version` and `model`.
- User overrides live in `<data folder>/prompts/overrides/<task>/prompt.md` and win over defaults; "reset" deletes the override.
- Prompts MUST NOT contain personal data, names or criteria; everything personal arrives through inputs.
- Responses are parsed as JSON and validated against the schema; one automatic retry with the validation error appended, then `InvalidModelOutput`.

The `practice` task (M2.17, ADR-076 item 2) is not in this table and not in `TASK_SPECS`: it is a fake-only vehicle for the task runner, offered only when fake providers are allowed and the fake is the saved provider. It never reaches a real model, produces no user-facing content and so cites no fact ids; this exemption is for that task alone.

### 3.7 Tiers and usage

Settings map two tiers, Fast and Thorough, to models offered by the active AI provider. Each task has a default tier; users can move tasks between tiers. Usage budgets are optional, per service and in requests (ADR-076 items 4 and 5). Token counts are recorded and shown; token budgets are deferred. Each budget has an allowance, a first and a second warning, a blocking point (at most the allowance), a reset day and optional per-tier caps. When a cap blocks a Thorough task, the app offers to run it once on Fast; it never switches silently. As built in M2:

- Services (ADR-079): `claude_subscription` (subscription token and existing sign-in together), `claude_api_key`, `web_search` and `fake` (only while fakes are allowed). Each has its own budget. The connection test counts one request on its provider's service, and is never warned or blocked (ADR-076 item 3).
- Cycles (ADR-076 item 8): a cycle starts at local midnight on the reset day, in the computer's time zone. Reset days 29 to 31 move to the last day of a shorter month. A service without a budget is shown by calendar month and is never blocked.
- Counting (ADR-079, ADR-080): a request is counted before it is made, in the same transaction as the block check, and completed afterwards whatever the outcome. An unfinished record still counts.
- Effective count (ADR-078, ADR-079): the higher of the local count and a provider-reported count, when that count has the budget's unit (requests) and window (month), and its reset is absent or equal to the cycle end. The Claude plan's utilization is shown for information only and is never compared.
- Warnings (ADR-076 item 7, ADR-086): each threshold is shown once per service and cycle, stored in `budget_notices`. The dialog says the number this request will reach ("This request will be number 10 of 40 this period"), because it counts the next request. The panel states the count used so far (ADR-085 item 1).
- Blocks (ADR-079, ADR-086): at the blocking point the start is refused before anything is queued, and the executor's reserve refuses the call too. The action is shown disabled, with the reset date.
- Unreadable budgets fail closed: nothing starts, and Settings shows a notice, never "No limit" (ADR-079, ADR-084).
- Saving (ADR-081, ADR-083): all budgets share one version; an all-empty save is refused; Remove clears a budget at once. "Background tasks at once" is stored and applies at the next start (ADR-082).
- Panel (ADR-085): each service shows its count against its allowance, its reset date, the unfinished calls, the local and provider counts with the one used, and a breakdown by tier and task.

### 3.8 Performance

Lists paginate (50 items). Indexes cover every list filter and sort. The inbox prefetches the next card. Fragment endpoints target under 150 ms server time.

### 3.9 Accessibility and responsiveness

WCAG 2.2 AA. Every interactive element is reachable by keyboard; every drag-and-drop has a keyboard alternative; after each htmx swap, focus moves deliberately and status changes are announced through an `aria-live` region; status is never shown by color alone; targets are at least 24 by 24 px; reduced-motion is respected. Layouts work from phone width (360 px) upward.

### 3.10 Language and copy

The UI ships in English. User-facing strings live in templates or a messages module so they can be translated later. Tone: plain, warm, never blaming ("Closed: rejected", not "failed").

---

## 4. Testing strategy

### 4.1 Test types

| Type | Location | Marker | What it covers | Speed target |
| --- | --- | --- | --- | --- |
| Unit | `tests/unit/` | none | Domain logic, prompt rendering, schema validation, honesty checker, budget math, state machines, detection, normalization | Whole suite under 30 s |
| Integration | `tests/integration/` | `integration` | Services with a real SQLite file in a temp folder, routes through FastAPI's TestClient (including middleware), migrations, task runner with fakes | Under 2 min |
| Contract | `tests/contract/` | `contract` | One shared suite every provider implementation must pass (AI, search, calendar, secrets), run against fakes and recorded transports | Under 1 min |
| End-to-end | `tests/e2e/` | `e2e` | Playwright against the real app started in a subprocess with a temp data folder and fake providers; desktop and phone viewports; keyboard-only paths; axe-core checks | Under 10 min |
| Smoke | `tests/e2e/test_smoke.py` | `smoke` | Start the app, `GET /health` and `/`, stop | Under 20 s |
| Failure injection | `tests/integration/failures/` | `integration` | Process killed mid-task, network errors, invalid model output, cancel during a call, budget exhaustion | Under 2 min |
| Property-based | alongside unit tests | none | Hypothesis tests for normalization, dedupe similarity, budget cycles, honesty number extraction | Bounded examples |
| Live | `tests/live/` | `live` | Real providers, opt-in only: requires `JOBPIPELINE_LIVE_TESTS=1` (env), `-m live` (pytest), and credentials read from `JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN` (required) and `JOBPIPELINE_LIVE_ANTHROPIC_API_KEY` (optional; API-key test skips without it); never in CI by default; minimal calls | Manual |

### 4.2 Fakes and fixtures

- `FakeAIProvider` (`app/modules/ai/providers/fake.py`): returns scripted responses keyed by task and fixture name, records every request for assertions, and simulates auth errors, usage limits, network failures, latency, invalid output and token usage. Selectable in the running app only when `JOBPIPELINE_ALLOW_FAKE_PROVIDERS=1` (spec 7.3; ADR-044, ADR-054), or on the demo folder under `--demo` (ADR-088).
- `FakeSearchProvider`, `FrozenClock` (`tests/fakes/`), `SequentialIdFactory` (`tests/fakes/`).
- Test keyring backends (`tests/fakes/keyring_backends.py`): an autouse fixture pins an in-memory backend for every test; a guard backend fails on any use outside test bodies (M1.2, ADR-046). `PYTHON_KEYRING_BACKEND` is pinned for the test session.
- Replay transport (`tests/fakes/claude_transport.py`): runs the Claude Agent SDK's real protocol and parser against hand-written fixture frames instead of a process (M1.1, ADR-045).
- `MemorySecretStore` (`app/modules/secrets/providers/memory.py`): an in-memory secret store used by contract and unit tests (ADR-044 item 2).
- Secret leak scan tools (`tests/leak_scan.py`): detects saved credentials in responses, logs, database and files; used in integration tests (M1.23, ADR-070). It covers every table found in `sqlite_master`, the raw files of the data folder, log records before redaction, log files, console output, responses with their headers, and the event stream (M2.26, ADR-088).
- Process kill and restart (M2.3, M2.21): `RunningApp.kill()` sends SIGKILL to the running app, and `start_app(restart=True)` starts it again on the same folders. `wait_until` (`tests/waiting.py`) polls against a deadline, never with a fixed sleep. Failure tests live in `tests/integration/failures/`.
- ASGI stream driver (`tests/asgi_stream.py`, ADR-077): reads `GET /events` as raw ASGI. httpx's `ASGITransport` buffers the whole response, and Starlette's `TestClient` blocks on a stream that never ends.
- Demo harness: `start_app(demo=True)` (`tests/running_app.py`) starts the app in demo mode, with the demo's folders redirected into a temporary home (checked on macOS only, M2.26). `tests/e2e/test_m2_demo.py` runs the real CLI with `--demo` the same way.
- E2E seeding: a test may open a running app's temporary database from the test process to seed rows, such as usage records (ADR-085 item 5).
- Axe: the e2e axe checks for the M2 screens run in light and dark, at desktop and phone sizes (M2.20, M2.22 to M2.25).
- All test data is fictional. Real names, real CVs and real personal data MUST NOT be committed.

### 4.3 Coverage

- At least 85% line coverage across `app/`, excluding live-only provider code paths.
- At least 95% branch coverage for critical modules: `app/domain/honesty.py` and `app/domain/fact_selection.py` (both from M3, ADR-089 item 5), `app/domain/budgets.py`, `app/services/usage.py`, `app/tasks/runner.py`, `app/onboarding/detection.py`, `app/modules/fetch/guard.py`, and the security middleware (`app/api/security.py`, ADR-039). `scripts/check_critical_coverage.py` enforces this with a registry of these modules and the milestone that introduces each (ADR-028).

### 4.4 What every feature's tests must include

- The happy path, at the right layer.
- Each acceptance criterion as at least one named test (`test_ac_m4_6_snooze_returns_card_as_new`).
- Error paths that the user can see.
- For UI work: an e2e scenario, a keyboard-only path where relevant, a phone-viewport check, and an axe scan with no serious or critical violations.
- For AI work: fixture responses including at least one malformed and one adversarial output.

### 4.5 Commands

| Purpose | Command |
| --- | --- |
| Install | `uv sync`, then `uv run pre-commit install` once per clone |
| Install browsers for e2e (once) | `uv run playwright install chromium` |
| Format and lint | `uv run ruff format .` and `uv run ruff check .` |
| Types | `uv run mypy` (checks `app` and `scripts`, from `pyproject.toml`) |
| Module boundaries | `uv run lint-imports` |
| Fast tests | `uv run pytest -m "not e2e and not smoke and not live"` |
| End-to-end | `uv run pytest -m e2e` |
| Smoke | `uv run pytest -m smoke` |
| Coverage | `uv run pytest -m "not e2e and not live" --cov=app --cov-report=term-missing --cov-fail-under=85` then `uv run python scripts/check_critical_coverage.py --from-progress` |
| Quick verification (all of the above except e2e and coverage) | `bash scripts/verify.sh quick` |
| Full verification | `bash scripts/verify.sh full` |

### 4.6 Continuous integration

A GitHub Actions workflow (created in M0) runs on every push and pull request:

1. Lint, format check, types, import contracts, gitleaks.
2. Unit, integration and contract tests on Ubuntu and macOS, with the oldest and newest supported Python versions; smoke tests on both. macOS runs on pull requests to `main`, manual dispatch and weekly (ADR-032). Windows is suspended (ADR-061): no CI job, no new Windows work; existing Windows code stays as it is.
3. Smoke and e2e tests on Ubuntu with Playwright Chromium.
4. Coverage thresholds, with `scripts/check_critical_coverage.py --from-progress`: a critical module due before the current milestone in `docs/PROGRESS.md` that is missing (for example after a rename) fails. The current milestone's own modules are not required in CI while it is in progress (in M0 nothing is); milestone acceptance runs `--milestone M<n>`, which requires them, as the backstop.

Keychain access is never exercised in CI; tests use `MemorySecretStore`, and the keyring adapter is tested with a fake keyring backend.

### 4.7 Definition of Done (per task)

1. Every acceptance criterion has a passing test.
2. Quick verification passes; full verification passes when the task touched UI, migrations or the task runner.
3. The code reviewer has no unresolved critical findings; the security and UX reviewers were consulted when their areas were touched.
4. `docs/PROGRESS.md` is updated with the evidence; any new decision is in `docs/DECISIONS.md`.
5. No secrets, personal data, skipped tests or untracked TODOs were introduced.

---

## 5. Roadmap and milestone test plans

Each milestone ends with a usable, verified increment. Milestones are planned into tasks with `/plan-milestone`, built with `/next-task`, and closed with `/milestone-acceptance`, which also lists the manual checks the user performs.

```mermaid
flowchart LR
  M0[M0 Foundation] --> M1[M1 Secrets, AI, setup guide]
  M1 --> M2[M2 Tasks and budgets]
  M2 --> M3[M3 Fact bank]
  M3 --> M4[M4 Inbox]
  M4 --> M5[M5 Board and Today]
  M5 --> M6[M6 Tailoring]
  M6 --> M7[M7 Enrichment]
  M7 --> M8[M8 Interview prep]
  M8 --> M9[M9 Polish and v1.0]
  M9 --> M10[M10 Automated sourcing]
```

| Milestone | Goal | Demo at the end |
| --- | --- | --- |
| M0 | Runnable, guarded skeleton | App starts, health check passes, all checks green |
| M1 | First-run experience with a working AI connection | Complete the setup guide with a real Claude connection |
| M2 | Reliable background AI work with optional budgets | Run, cancel and recover tasks; see budget warnings |
| M3 | Fact bank with CV import | Import a CV, review drafts, refine a fact with AI |
| M4 | Job intake and triage | Add jobs, triage the queue with the keyboard |
| M5 | Tracking | Move applications on the board; see Today |
| M6 | Honest tailoring and documents | Produce, review and export a tailored CV and letter |
| M7 | Ongoing enrichment | Run a short session that adds facts |
| M8 | Interview preparation | Generate, practice and debrief |
| M9 | Version 1.0 | Full accessibility pass, export/import, demo mode |
| M10 | Automated sourcing | New postings from watched companies appear in the inbox |

### M0 — Foundation and tooling

**Goal:** a runnable, verifiable skeleton with every guardrail in place before any feature work.

**Scope:**

- `pyproject.toml` managed by uv; ruff, mypy, pytest (markers from 7.2), pytest-cov, hypothesis, import-linter contracts for 2.3, pre-commit with ruff and gitleaks; GitHub Actions workflow (4.6).
- App factory and lifespan (open database, apply migrations, start the task runner stub); base template with vendored htmx; CSS tokens for light and dark themes; responsive app shell (sidebar collapses at narrow widths).
- `jobpipeline` CLI: `--port`, `--data-dir`, `--no-browser`, `--setup`, `--demo`, `--version`; opens the browser once the server responds.
- Bootstrap config and data folder resolution (`platformdirs`); demo mode uses its own folder.
- Database module, migration runner and migration `0001` (settings, setup_state, tasks, usage_records, schema_migrations). Every mutable table carries `created_at`, `updated_at` and `version` (2.7), even where the detailed design's table list omits them. No full-text search tables yet; the active-task dedupe index and `usage_records.task_id` come in M2 (ADR-027).
- Middleware: host check, launch token, origin check; binding to 127.0.0.1.
- Logging with redaction; `/health`.
- `app/domain` skeleton with `Clock` and `IdFactory`; module protocols from 2.4; fakes in `tests/fakes/`.
- `scripts/check_critical_coverage.py`; the smoke test; `.claude/skills/verify/scripts/verify.sh` running green.

**Acceptance criteria:**

- AC-M0.1 On a fresh clone, `uv sync` followed by `uv run jobpipeline --no-browser --data-dir <tmp>` serves `GET /health` with 200 within 5 seconds.
- AC-M0.2 A request whose Host header is not 127.0.0.1 or localhost with the right port is rejected.
- AC-M0.3 A fragment or POST request without the launch token gets 403; with it, the request is handled.
- AC-M0.4 Migrations create the schema on an empty database, are a no-op when re-run, and back up an existing database before applying new ones.
- AC-M0.5 Secret values registered with the redaction filter, and strings shaped like Anthropic keys or OAuth tokens, never appear in log output.
- AC-M0.6 `uv run lint-imports` passes; a fixture module that violates a contract is detected (contract self-test).
- AC-M0.7 Quick verification passes locally and CI is green on all three operating systems.

**Test plan:** unit tests for config precedence, token generation, redaction and migration ordering; integration tests for middleware cases and `/health`; smoke test via subprocess; manual check that gitleaks blocks a staged file containing a fake key.

**Exit checks:** full verification; the user opens the app and sees the empty shell in light and dark themes and at phone width.

### M1 — Secrets, AI provider and the setup guide

**Goal:** a new user can launch the app and, guided step by step, reach a working Claude connection.

**Scope:**

- Secrets module: keyring store, environment fallback, memory store for tests.
- AI module core: provider protocol, prompt loader and renderer with the untrusted filter (3.3), JSON schema validation with one retry, tier resolution.
- Claude provider built on the Agent SDK, supporting a subscription token (`claude setup-token`, passed as `CLAUDE_CODE_OAUTH_TOKEN` only to the SDK call's environment), an existing Claude Code sign-in, and an API key; isolation rules from 2.9; error mapping from 2.11; `test_connection` with one minimal request.
- Fake AI provider, selectable when allowed (4.2).
- Onboarding module (`app/onboarding/`): startup detection with the four cases in the detailed design; redirect middleware (303 for pages, `HX-Redirect` for htmx); step registry; steps 1 (welcome and data folder), 2 (AI provider), 5 (job preferences) and 6 (done). Steps 3 and 4 register in M2 and M3 (step 3, web search, was delivered in M2.25; ADR-087).
- Settings > Providers: change provider, replace key, test connection, map tiers.

**Acceptance criteria:**

- AC-M1.1 The four startup states (no bootstrap config; config but no database; database with incomplete setup; complete setup with a broken provider) each lead to the specified behavior.
- AC-M1.2 While setup is incomplete, every route except the guide, static files and `/health` redirects to the guide.
- AC-M1.3 With the fake provider, valid credentials show "Connected"; auth failure, CLI not found, usage limit and network failure each show their mapped message.
- AC-M1.4 No response, page, log or database row ever contains a saved secret (automated scan in integration tests).
- AC-M1.5 Without a keychain, the provider step explains the environment fallback, and the app works with the variable set.
- AC-M1.6 Each step saves when valid; restarting the server mid-setup resumes at the first unfinished required step.
- AC-M1.7 `jobpipeline --setup` reopens the guide without losing data; the Done step lists skipped steps.
- AC-M1.8 The guide completes with keyboard only, works at 390 by 844 px, and passes axe with no serious or critical violations.
- AC-M1.9 (live, opt-in) `test_connection` succeeds with a real subscription token and with a real API key.

**Test plan:** unit tests for detection, step ordering, error mapping and secret fallback; contract tests for secret stores and the AI provider against fakes; integration tests for routes, persistence and redirects; e2e scenarios for the full guide (desktop, phone, keyboard-only, axe); live test AC-M1.9 run by the user.

**Exit checks:** full verification; the user completes the guide with their own subscription token and once with an API key if they have one.

### M2 — Task runner, live updates and usage budgets

**Goal:** every AI action runs as a reliable, visible, cancellable background task, under optional budgets.

**Scope:**

- Task runner implementing these reliability rules:
  1. One source of truth: the tasks table, with states queued, running, succeeded, failed, cancelled, interrupted; every transition is a conditional update that succeeds only from the expected state.
  2. No duplicate work: a dedupe key (task type plus target); enqueueing an active duplicate returns the existing task.
  3. Atomic results: output and the succeeded state are written in one transaction; results are always new drafts, never overwrites.
  4. Crash recovery: on startup, running tasks become interrupted; failed and interrupted tasks are never retried automatically.
  5. Hung calls: a heartbeat while running; a watchdog interrupts tasks whose heartbeat stops beyond the task type's timeout.
  6. Cancellation: a flag checked between steps; the provider call is aborted where possible; partial output discarded.
  7. Usage accounting: a usage record is reserved before each external call and completed after it.
  8. Reconnecting pages re-fetch task state, so missed events don't matter.
  9. SQLite in WAL mode with a busy timeout and short transactions. The runner works on the event-loop thread, so its database calls go through `asyncio.to_thread` and nothing is awaited inside `Database.transaction()`: the transaction lock is per thread (ADR-038), so two coroutines interleaving inside one transaction would collide, and a blocking call would stall the loop.
  10. Failure tests for each of the above.
- A migration adding the unique partial index on active `tasks.dedupe_key` (queued or running) and a `task_id` link from `usage_records` to `tasks` (deferred from M0, ADR-027).
- An event stream at `GET /events` (`text/event-stream`) and a fetch-based live-update reader (no htmx SSE extension, no `EventSource`; ADR-077); top-bar task indicator and tasks panel. The browser's `EventSource` can't send the `X-JP-Token` header, so the stream is authorised as every other route is and read with `fetch`; decided in ADR-077 (never with the token in the URL; ADR-039).
- Usage service: optional budgets per service (ADR-076 item 4; allowance, two warnings, blocking point, reset day), per-tier caps, provider-reported usage where available (the higher count wins when unit and window match; ADR-078), warning dialogs once per threshold per cycle, blocking with alternatives, budget hook for web search tool calls (wired before M8; ADR-080).
- Setup step 3 (web search and optional budget; ADR-087); Settings > Usage budgets; usage breakdown by tier and task (ADR-081 to ADR-086).

**Acceptance criteria:**

- AC-M2.1 Enqueueing a duplicate active task returns the existing task id.
- AC-M2.2 Killing the process during a task leaves it interrupted after restart, with a working retry and no automatic retry.
- AC-M2.3 With a frozen clock, a task whose heartbeat stops is interrupted after its timeout.
- AC-M2.4 Cancelling discards partial output and ends in cancelled.
- AC-M2.5 An injected failure between writing a result and marking success leaves no orphaned draft.
- AC-M2.6 A crash during a provider call still leaves the reserved usage record counted.
- AC-M2.7 Threshold validation (first < second < block ≤ allowance); each warning appears once per cycle; the block disables the action with the reset date; cycles reset correctly on short months and reset days of 29–31.
- AC-M2.8 When the provider reports higher usage than the local count, the provider's number is used.
- AC-M2.9 After the event stream drops and reconnects, the tasks panel shows correct states.
- AC-M2.10 A provider without a budget is never blocked or warned.

**Test plan:** unit and Hypothesis tests for budget math and cycles; integration tests for the runner with the fake provider and injected exceptions; failure-injection tests with a real subprocess kill; e2e for the tasks panel, cancel and warning dialogs.

**Exit checks:** full verification, critical coverage for the runner and usage modules.

**Notes (decided during M2; the criteria above are unchanged):**

- Scope moves (ADR-076 item 12): the notifications list and Today's list of skipped steps move to M5. The per-task tier list stays deferred to M3 (ADR-069). The design's `GET /api/tasks/{id}` and `GET /api/usage` are fragment routes (`/tasks/{id}`, `/settings/usage`), with JSON in M9. Wiring the web-search budget hook into the Claude provider stays before M8 (ADR-057); until then the provider refuses web-search requests (ADR-080).
- Budgets are per service and count requests (ADR-076 items 4 and 5). Token budgets are not in M2.
- Setup step 3 (web search, optional, with an optional budget and "Skip for now") is in (M2.25, ADR-087). The guide has five steps: welcome, AI provider, web search, job preferences, done. The step numbers in the M1 scope above are M1's own.
- The practice task (fake-only, ADR-076 item 2) drives the runner, the tasks panel and the budget dialog until M3, when the first real task type arrives. `TASK_SPECS` stays empty until M3.
- Settings > Usage budgets, the usage panel and the warning dialog with blocked actions are built as M2.22 to M2.24 describe (ADR-081 to ADR-086).
- AC-M2.8 is proven with the fake provider's scripted snapshots (ADR-076 item 6). The Claude plan's utilization is information only.
- AC-M2.9's panel check is proven with the panel (M2.20), as the M2.18 note says (ADR-077).
- AC-M2.10: "provider" means a service (ADR-076 item 4).
- AC-M1.4 is extended to tasks, usage rows, events and logs (M2.26, ADR-088). AC-M1.7 has a real skipped step from M2.25: web search (ADR-087).
- Background tasks at once (1 to 3) applies at the next start (ADR-082).
- Known limit: heartbeats come only from handler steps, and nothing in the AI module beats during a provider call. A task type's timeout must therefore cover its longest call until such a hook exists (M2.10).

### M3 — Fact bank and CV import

**Goal:** the user's experience is captured as reviewable, versioned facts and stories.

**Scope:** roles, facts, stories, versions, skills from the controlled list, privacy options (never use, generalize names); drafts review (confirm, edit, merge, discard, batch confirm by source); library with filters and full-text search; editor with history and diff; capture inbox (basic); CV import (PDF, Word, pasted text → text extraction → `extract_cv` → drafts); setup step 4; Refine with AI (`refine_fact`); near-duplicate suggestions on demand.

**Acceptance criteria:**

- AC-M3.1 CV import produces only drafts; nothing becomes confirmed without a user action.
- AC-M3.2 Confirm, edit-then-confirm, merge, discard and batch confirm all work and are versioned.
- AC-M3.3 Every edit creates a version; the history view shows the diff.
- AC-M3.4 Facts marked "never use" never appear in any AI request (checked through the fake provider's request log); "generalize names" adds the instruction.
- AC-M3.5 Refine with AI: a fake response containing an invented claim is flagged and is never applied without explicit acceptance.
- AC-M3.6 Full-text search and filters by role, skill and status return correct results.
- AC-M3.7 Oversized, wrong-type and malformed uploads fail with friendly messages and leave no partial records.
- AC-M3.8 Setup step 4 works and, when skipped, is listed on the Done screen and Today.

**Test plan:** unit tests for extraction adapters with fictional fixture files and version logic; integration tests for the import pipeline with fake AI fixtures (including malformed output); e2e for import, review, confirm and refine, with axe and keyboard checks.

**Exit checks:** full verification; the user imports their own CV locally (never committed) and reviews the drafts.

### M4 — Job intake, inbox and companies

**Goal:** jobs arrive, are summarized and assessed honestly, and can be triaged quickly.

**Scope:** fetch module with the SSRF guard; manual intake by URL or pasted text; description snapshot; `summarize_job` and `assess_fit`; structured fields including industry, department or team, experience required and salary; normalization to controlled lists with aliases; deduplication with merge and split; inbox with queue and focused card, seen state, snooze, removal reasons, undo, shortcuts (J, K, A, R, Enter) and next-card prefetch; job detail drawer with fit explained through fact links and editable fields that survive re-summarizing; companies (entity, page, list); saved views; deal-breaker flags.

**Acceptance criteria:**

- AC-M4.1 URLs resolving to private, loopback, link-local or multicast addresses, non-http schemes, and redirects to any of these are refused with the paste-text message.
- AC-M4.2 The snapshot and source link are stored at intake; pasting text works when fetching fails.
- AC-M4.3 Fit bands follow the definitions; matched and missing requirements cite fact ids; deal-breakers are flagged, not hidden.
- AC-M4.4 Aliases normalize to controlled values; unknown values are queued for the user to map.
- AC-M4.5 Fixture duplicates merge above the similarity threshold; "not a duplicate" splits them.
- AC-M4.6 A card is marked seen when shown; a snoozed card returns as new on its date; removal accepts an optional reason; undo works within its window.
- AC-M4.7 The whole triage flow works by keyboard and announces each action to screen readers.
- AC-M4.8 Cards show industry, department or team, experience required and salary when present.
- AC-M4.9 User-corrected fields are preserved when a job is re-summarized.
- AC-M4.10 With 1,000 jobs, inbox fragments render under 150 ms server time.

**Test plan:** unit and property tests for the SSRF guard, normalizer and dedupe similarity; integration tests for the intake pipeline with fake fixtures (including a job posting containing prompt-injection text, which must not change the task's behavior); e2e for triage on desktop and phone and by keyboard; a performance test with generated data.

**Exit checks:** full verification; the user triages a few real postings locally.

### M5 — Applications board, tracking and Today

**Goal:** every application is tracked with status, events, contacts and reminders.

**Scope:** applications created by Apply; configurable statuses, each belonging to a phase (preparing, active, closed); board with columns, SortableJS drag-and-drop, a keyboard status menu, collapsed Closed column and a table view; application drawer; contacts and interaction log; events with time zones; calendar module with the .ics provider (interface ready for future sync providers that store external event ids); in-app reminder rules; Today screen; in-app notifications list.

**Acceptance criteria:**

- AC-M5.1 Status changes are recorded in history; custom statuses must belong to a phase.
- AC-M5.2 Drag-and-drop and the keyboard status menu produce identical results.
- AC-M5.3 Moving to Interviewing offers an optional date; moving to Rejected offers optional feedback.
- AC-M5.4 Generated .ics files parse correctly and keep the right time zone.
- AC-M5.5 With a frozen clock, due reminders appear on Today and in notifications; nothing runs while the app is closed.
- AC-M5.6 Today shows its blocks in priority order, hides empty ones, and says plainly when nothing is due.

**Test plan:** unit tests for reminder rules and .ics generation; integration tests for status history and phases; e2e for the board (drag and keyboard), drawer and Today, with axe and phone viewport.

**Exit checks:** full verification.

### M6 — Tailoring, honesty enforcement and documents

**Goal:** honest, reviewable, exportable CVs and cover letters for each application.

**Scope:** workspace stepper; `gap_check`, `propose_angles`, `tailor_cv`, `draft_cover_letter`; the honesty checker (3.1); diff view; edit preservation on regenerate; "save as fact?" for typed claims; country templates (`draft_cv_template`, review, reuse, optional photo slot and upload); renderer to PDF and Word; exports; submit and freeze; "used in" quick links in the fact bank. The milestone starts with a renderer spike that compares WeasyPrint with alternatives on all three operating systems and records an ADR.

**Acceptance criteria:**

- AC-M6.1 Every generated content unit cites confirmed fact ids; uncited or unknown citations are flagged and block approval until resolved.
- AC-M6.2 Numbers absent from the cited facts are flagged for checking.
- AC-M6.3 Regenerating preserves user edits unless "replace everything" is confirmed.
- AC-M6.4 Typing a new claim into a document triggers "Save this as a fact?".
- AC-M6.5 A country template is drafted once, approved, and reused; a photo appears only when the template has a slot and the user uploaded one.
- AC-M6.6 PDF and Word exports open and contain exactly the approved content (verified by text extraction).
- AC-M6.7 Marking an application Applied freezes document and fact versions; later edits create new versions; the drawer shows what was sent.
- AC-M6.8 When must-haves are missing, the gap check says plainly that the role may not be worth applying for (fixture).

**Test plan:** unit tests for the honesty checker including every adversarial case, and for diffing; integration tests for the tailoring pipeline with fixtures; e2e for the full workspace flow; renderer tests on the CI operating-system matrix.

**Exit checks:** full verification; the user produces and reads one real tailored CV locally.

### M7 — Enrichment sessions and coverage

**Goal:** short, optional, recurring sessions grow the fact bank.

**Scope:** question backlog with the priority signals from the detailed design; session screen; `enrichment_question` and `structure_answer`; outcomes (answer, skip, not sure, ask me later with its menu); cooldowns; end-of-session summary; coverage view; capture inbox expansion; frequency settings and pause; entry points from applications, interviews and rejections.

**Acceptance criteria:**

- AC-M7.1 Backlog order follows the priority signals (fixture scenarios).
- AC-M7.2 Questions are open and non-leading: the prompt's golden test passes and generated questions in the opt-in live evaluation contain no embedded claims about the user.
- AC-M7.3 Each answer produces a draft and keeps the original answer.
- AC-M7.4 "Ask me later" choices and cooldowns schedule correctly (frozen clock).
- AC-M7.5 Coverage strength matches the definitions (Strong, Thin, None); selecting a row starts a focused session.
- AC-M7.6 Suggestions respect the configured frequency; pause stops them.

**Test plan:** unit tests for priority scoring and scheduling; integration tests for sessions with fixtures; e2e for a full session and the coverage view.

**Exit checks:** full verification; the user runs one real session.

### M8 — Interview preparation

**Goal:** on-demand preparation that reuses past work.

**Scope:** prep tab; question generation from the job, the CV sent, and questions to ask them; question bank tagging and reuse ranking (detailed design); web questions through the search module (delegated web search with the budget hook), labeled with source and date; practice with `answer_feedback` and the honesty check; narrative cards (`draft_narrative`); debrief flow; `adapt_answer`; on-demand company brief; prep state on the board and Today; speech buttons only when the provider declares speech capabilities.

**Acceptance criteria:**

- AC-M8.1 For a new application, matching bank questions rank by the defined relevance, and new questions are generated only for uncovered requirements (checked through the fake provider's request log).
- AC-M8.2 Web questions run only on an explicit click; every web search tool call is counted; at the blocking point the web button is disabled with the reset date while regular generation still works.
- AC-M8.3 Reported-online questions carry their source and date; output derived from a provider whose storage rule is unclear is not cached.
- AC-M8.4 Practice feedback flags claims not in the fact bank and offers to add them as drafts.
- AC-M8.5 A debrief creates bank entries, draft facts and events, and suggests a status change the user confirms.
- AC-M8.6 Speech controls are hidden when the provider lacks the capability.

**Test plan:** unit tests for ranking; integration tests with fixtures and the budget hook; e2e for generate, practice and debrief.

**Exit checks:** full verification; the user prepares for one real or mock interview.

### M9 — Polish and version 1.0

**Goal:** a complete, accessible, shareable first release.

**Scope:** learning from removals (six-step design: mapping, explainable detection with a threshold, previewed suggestions, visible and reversible rules, no nagging, `group_removal_reasons` on request); prompts editor with overrides; lists and statuses editor; appearance settings; JSON export and import with a schema version (keys excluded); delete all data; demo dataset; full accessibility audit; performance pass; README and user guide; tag v1.0.

**Acceptance criteria:**

- AC-M9.1 Removal suggestions appear only above the threshold and without contradicting applications; flag is the default; rules are visible, editable and reversible; hidden jobs remain reachable.
- AC-M9.2 Prompt overrides survive updated defaults; reset restores the default.
- AC-M9.3 Export then import into a fresh data folder reproduces the data exactly (round-trip test), without keys.
- AC-M9.4 Demo mode never reads or writes the real data folder.
- AC-M9.5 axe reports no serious or critical violations on any page; the manual keyboard and screen-reader checklist passes.

**Test plan:** unit tests for suggestion rules; round-trip integration test; e2e across all pages with axe; manual checklist in `docs/acceptance/M9.md`.

**Exit checks:** full verification on all operating systems; the user reviews the README as a newcomer would.

### M10 — Automated job sourcing (after v1.0)

**Goal:** postings from watched companies arrive in the inbox without manual intake.

**Scope:** a sourcing interface with providers for the public job board endpoints of Greenhouse, Lever and Ashby; per-company watch settings; checks while the app is open plus "check now"; deduplication against existing jobs; per-source error isolation; respect for each source's terms and rate limits.

**Acceptance criteria:**

- AC-M10.1 New postings from a watched company appear as unseen cards with their source.
- AC-M10.2 Postings already in the inbox are deduplicated.
- AC-M10.3 One failing source never blocks others; errors are visible in the tasks panel.
- AC-M10.4 No source scrapes a site whose terms forbid it; each provider documents its terms check.

**Test plan:** contract tests per source with recorded responses; integration tests for dedupe and isolation.

---

## 6. The agentic development system

Claude Code and Codex share the project contract, procedures, verification and repository handoffs. Each tool uses native skills, agent definitions, hooks and permissions (ADR-096, superseding ADR-021). Detailed setup and maintenance: `docs/development/agents.md`. This changes development tooling; the application's Claude Agent SDK provider is independent.

### 6.1 Overview

One main session owns a checkout and coordinates the task. Claude uses its existing specialists; Codex normally executes development phases in the main session and delegates independent reviews. Both follow tests first, implementation, verification, review, fixes/re-verification, evidence and a task commit. Milestone acceptance needs the user's manual confirmation.

### 6.2 Building blocks

| Block | Shared source | Native loading |
| --- | --- | --- |
| Project contract | `AGENTS.md` | Codex reads it; `CLAUDE.md` imports it for Claude |
| Engineering rules | `.claude/rules/*.md` | Claude path matching; Codex explicitly reads matching rules from the AGENTS.md routing table |
| Procedure skills | Six procedure directories under `.agents/skills/` | Codex discovery; Claude skill wrappers load the shared body/references |
| Workflow skills | Tool-specific orchestration, same gates | `.claude/skills/` and `.agents/skills/` |
| Specialists | Shared role checklists in `.claude/agents/` | Claude Markdown agents; four Codex TOML reviewer profiles read the checklists |
| Verification/status | `scripts/verify.sh`, `scripts/next_task.py`, `scripts/session_context.py` | Both tools; old Claude paths remain compatible |
| Hook logic | `scripts/agent_support.py`, `scripts/agent_hook.py` | `.claude/settings.json` + wrappers; `.codex/hooks.json` |
| Native permissions | Same project authorization boundaries | Claude allow/ask/deny; Codex config, command rules, sandbox and approvals |
| Handoffs | `docs/PROGRESS.md`, `docs/DECISIONS.md`, `docs/acceptance/`, `docs/development/review-notes/` | Read on demand; status also injected by SessionStart |

### 6.3 Subagents

Claude retains `planner`, `test-author`, `implementer`, `verifier`, `debugger`, `code-reviewer`, `security-reviewer`, `ux-reviewer`, `prompt-engineer`, `docs-keeper` and their existing tool/model settings. The docs keeper edits documentation only and needs explicit authorization to change the spec or reference designs.

Codex's main session handles planning, tests, implementation, verification, debugging and records. It delegates an independent code review and triggered specialist reviews through `.codex/agents/` (`code-reviewer`, `security-reviewer`, `ux-reviewer`, `prompt-reviewer`). Profiles inherit the selected model, provider and permissions; role instructions do not create separate read-only sandboxes. When a client cannot select a profile, pass the same instructions to a generic subagent. If delegation is unavailable, independent review is outstanding, not replaced by self-review.

| Touched | Required independent review |
| --- | --- |
| Every implementation task | Code reviewer |
| Secrets, providers, fetching, uploads, middleware, credentials, coding-tool hooks/permissions | Security reviewer |
| Templates, static files, page routes | UX reviewer |
| Prompts, AI task contracts and fixtures | Prompt engineer in review mode (Claude) / prompt reviewer (Codex) |

Pass the task, criteria, relevant rules, diff scope and actual verification evidence to each reviewer. Fix critical findings; record unresolved non-critical findings. The main session records recurring reviewer lessons in shared notes; Claude's code reviewer may update those notes directly as its only non-native-memory edit.

### 6.4 Skills

| Skill name | Purpose |
| --- | --- |
| `plan-milestone` | Propose small testable tasks; write the plan only after approval |
| `next-task` | Full development loop for exactly one task |
| `run-milestone` | Eligible tasks back to back, one commit each, checkpoints and parking (ADR-065) |
| `verify` | Shared quick/full recipe before committing or claiming completion |
| `milestone-acceptance` | Full verification, criteria-to-tests report and user manual checklist |
| `add-ai-task` | Prompts, schemas, fixtures, task contract, honesty |
| `add-provider` | Interface/capability/secrets/budget/contract checks |
| `htmx-ui` | Fragment, focus, announcements, keyboard and live-update conventions |
| `db-migration` | Forward-only schema/data migrations and backup/equality tests |
| `background-task` | Task runner reliability and budget rules |
| `onboarding-step` | Registry, detection, persistence, redirects and tests |

Claude uses `/name`; Codex uses an attached skill or `$name` with ids/options in the request. Planning, task/milestone execution and acceptance require explicit invocation in both tools. Verification and procedures can be selected automatically. Codex invocation controls live in `agents/openai.yaml`; Claude's extensions remain in its own wrappers/workflows. Shared instructions contain no Claude argument substitutions or injected shell syntax.

### 6.5 Hooks and permissions

| Event | Behavior |
| --- | --- |
| SessionStart | Reload current milestone/open tasks and shared golden rules on startup, resume, clear and compaction; Codex also covers forks |
| PreToolUse | Normalize Claude tools and Codex patches/shell aliases; check secrets, real data/config folders, common dangerous/secret-printing commands and guardrail edits |
| PostToolUse | Format/lint existing changed Python files, including patch rename destinations, using offline no-sync uv; report remaining errors |

Codex patches carry their text in `tool_input.command`; renamed source/destination and deleted paths are checked. Malformed policy input or unrecognized file/patch shapes block with a reason. Informational/post-edit failures are advisory; explicit verification still gates completion. Hooks catch common mistakes and complement native permissions/sandbox and pre-commit/CI; arbitrary shell programs and every connector path are not fully interpreted or covered.

Claude keeps native allow/ask/deny rules. Codex config requests workspace-write, on-request approval, no sandbox network access, enabled hooks and up to three concurrent subagents. Native command rules request approval for common push/history/dependency/deletion commands. Higher-priority managed policies still apply. Project hook definitions require trust review in Codex; skipped/untrusted hooks are not active protection.

Guardrail changes need the user's explicit authorization. Claude can return `permissionDecision: ask`; current Codex cannot, so its adapter blocks and points to the maintenance instructions. After approval the user can temporarily disable only the project pre-tool hook in the native hook UI, re-enable it after the authorized edit, review/trust definitions and re-run synthetic probes. The agent never disables hooks or bypasses trust/sandbox on its own. No permanent maintenance environment switch.

### 6.6 The development loop

1. Plan from spec section 5 and the design. Propose criteria, tests, touched modules and checkpoints; wait for approval before writing tasks.
2. Inspect branch/status/history and resume task-owned work at its actual phase. Stop if ownership of changes is unclear.
3. Write behavior tests first and observe expected failures using fictional fakes/temp data. Claude delegates to test-author; Codex normally performs this phase itself.
4. Implement using the relevant shared procedures; Claude may delegate to implementer.
5. Run `bash scripts/verify.sh quick`, or full for templates/static/page routes/migrations/task runner changes. Claude normally delegates execution to verifier. Debug within scope; stop after three failed fixes of the same failure.
6. Obtain independent code review and the triggered specialist reviews; fix critical findings and cheap should-fix findings, record the rest, and re-verify after fixes.
7. Record task completion/evidence, leftovers and a dated session-log entry. Add an ADR for decisions. Stage only task-owned files and commit with a conventional task-id message; never bypass pre-commit.
8. Report and stop after one task, or continue eligible tasks under run-milestone/ADR-065. Local problems may be parked only with task-owned changes safely stashed and a recorded Blocked question; never stash another agent's work. Global problems/checkpoints/caps stop the run.
9. When all tasks are complete, run full verification and milestone-specific critical coverage. Map criteria to observed passing tests in `docs/acceptance/M<n>.md`. Present the user's manual checklist and wait for confirmation before accepting the milestone or advancing Current milestone. Suggest the user merge; no automatic merge/push.

### 6.7 When agents must stop and ask you

- Sources conflict, or the spec is ambiguous.
- A new runtime dependency or any stack change.
- Any change to the honesty rule, the security model, or stored personal data.
- Scope changes to a milestone.
- A test's expectation seems wrong; never weaken tests to get green.
- Three failed fixes of the same failure.
- Anything needing real credentials or the user's real data.
- Pushing, rewriting history or deleting data.

Existing authorization for the specific change remains valid; do not ask repeatedly for the same approved scope. Native sandbox/trust approval can still be required.

### 6.8 Shared memory and handoffs

- `PROGRESS.md` retains its parsed format: Current milestone, milestone headings, checkbox task ids, indented AC/Tests/Evidence and optional Checkpoint/Blocked lines, then a session log (ADR-065).
- ADRs append context/decision/consequences/date in `DECISIONS.md`; acceptance reports remain under `docs/acceptance/`.
- Recurring review lessons live in `docs/development/review-notes/`. Native Claude memory files route there; do not fork a second body of lessons.
- Prefer switching agents at committed task boundaries. Unfinished handoffs name the task/phase, changed files, actual tests/results, outstanding review, blockers/checkpoints and next action. The receiving agent inspects the diff before resuming.
- Integrated work stays on one milestone branch (`m<n>-<slug>`), conventional commits include the task id, and the user merges after acceptance.

### 6.9 Parallel work

One writer per checkout. Parallel implementation needs separate worktrees, disjoint files and one coordinator that alone updates shared progress and integrates verified/reviewed task commits. Never parallelize migrations, the same module or shared templates.

### 6.10 Usage awareness

Delegate where independent judgment, useful isolation or parallel work justify it. Subagents consume the same account limits. Claude's existing lighter verifier/docs models remain; Codex starts with model inheritance rather than hard-coded substitutions for Sonnet/Haiku. Keep skill descriptions precise and detailed references on demand.

### 6.11 Permission modes

Use normal native permission/approval modes and workspace trust. Codex requests workspace-write/on-request; managed policies may override it. Do not use bypass mode or bypass hook trust. Cache/browser downloads and local server tests can need targeted command approval; this does not authorize live-provider calls.

### 6.12 First session in a clone or worktree

Follow `docs/development/agents.md`: install dependencies/pre-commit and browsers as needed, review/trust project configuration and hooks, confirm instructions/skills/current status in a fresh session, then run shared verification. Resume the existing Current milestone rather than restarting the roadmap. Validate hook event dispatch separately from directly invoking the adapter.

### 6.13 Evolving the system

Keep shared rules/procedures/lessons in one maintained place and native wrappers small. Encode recurring issues in the appropriate rule, procedure or tested hook policy. Validate changed skills and both payload formats, prune stale review lessons, and re-review changed hook scripts as well as native hook definitions. Keep the root guide concise; use on-demand references for detail.

---

## 7. Appendices

### 7.1 App command

`jobpipeline [--port N] [--data-dir PATH] [--no-browser] [--setup] [--demo] [--version]`

### 7.2 Test markers

| Marker | Meaning |
| --- | --- |
| `integration` | Uses real SQLite and HTTP routes, with fake providers |
| `contract` | Shared provider contract suites |
| `e2e` | Playwright against the running app |
| `smoke` | Start, health check, stop |
| `live` | Real providers; skipped unless `JOBPIPELINE_LIVE_TESTS=1` |
| `slow` | Longer property or performance tests |

### 7.3 Environment variables and defaults

| Variable | Purpose | Default |
| --- | --- | --- |
| `JOBPIPELINE_DATA_DIR` | Data folder override; beats `--demo`, and the `--data-dir` flag beats it (ADR-025). When the folder is set by this variable, `--data-dir` or `--demo`, it can't be changed in step 1 | Precedence: `--data-dir` > `JOBPIPELINE_DATA_DIR` > `--demo` > bootstrap config > default |
| `JOBPIPELINE_CONFIG_DIR` | Config folder override for tests and manual checks. `config.toml` is parsed in demo mode too; demo mode ignores its `data_dir` and never writes it (ADR-025, ADR-044 item 4) | `user_config_dir("jobpipeline")` |
| `JOBPIPELINE_PORT` | Port override (1–65535, invalid values are `ConfigError`) | 8000 |
| `JOBPIPELINE_LOG_LEVEL` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, case-insensitive) | `INFO` |
| `JOBPIPELINE_ALLOW_FAKE_PROVIDERS` | Exactly `"1"` makes fake providers selectable in the running app (tests and demos only; ADR-054). `--demo` on the demo folder offers the fake and the practice task without it; with `--data-dir` or `JOBPIPELINE_DATA_DIR` the variable is still needed (ADR-088) | unset |
| `JOBPIPELINE_LIVE_TESTS` | Exactly `"1"` enables live tests, which also need `-m live` (spec 4.1) | unset |
| `JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN` | Claude subscription token for live tests only (never read from keychain, ADR-044 item 10) | unset |
| `JOBPIPELINE_LIVE_ANTHROPIC_API_KEY` | Anthropic API key for live tests only (never read from keychain; test skips if unset, ADR-044 item 10) | unset |
| `JOBPIPELINE_E2E_CHROMIUM` | Path to a Chromium executable for e2e tests instead of Playwright's managed one (tests only; ADR-035) | unset |
| `ANTHROPIC_API_KEY` | Fallback credential when no keychain is available; stripped of whitespace; read-only (spec 2.9; ADR-047) | unset |
| `CLAUDE_CODE_OAUTH_TOKEN` | Fallback credential when no keychain is available; stripped of whitespace; read-only (spec 2.9; ADR-047) | unset |
| `PYTHON_KEYRING_BACKEND` | keyring library variable, honoured by the running app too: selects a keyring backend by class name; tests use it, and an autouse fixture pins an in-memory backend per test (ADR-046) | unset (uses OS discovery) |

Precedence for AI provider credentials (ADR-047): keychain (service `jobpipeline` in normal mode, `jobpipeline-demo` in demo mode) beats environment variables (`ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`). Demo mode never reads environment credentials: wiring gives it an empty environment. Live tests read only from `JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN` (required) and `JOBPIPELINE_LIVE_ANTHROPIC_API_KEY` (optional), never from the keychain or other variables (ADR-044 items 7 and 10).

All credential values are registered with the log redactor; the app never writes them to logs or responses (spec 2.10, 3.3; ADR-037, ADR-070).

### 7.4 Glossary

- **Fact:** a single statement about the user (skill, tool, metric). **Story:** situation, action, result. Both are versioned.
- **Draft / approved / frozen:** AI output starts as a draft, becomes approved by the user, and is frozen when sent.
- **Angle:** a positioning chosen per application or role type.
- **Tier:** Fast or Thorough, each mapped to a model.
- **Budget, cycle:** an optional usage limit per service that resets on a reset day.
- **Task:** a background unit of work with a dedupe key and a terminal state.
- **Seen, snoozed:** inbox states replacing a "Later" button.
- **Phase:** preparing, active or closed; every status belongs to one.
- **Controlled list:** the allowed values for a structured field, with aliases.
- **Untrusted content:** any text that did not come from the user's own direct input, delimited before reaching a prompt.
- **Setup guide:** the onboarding module that runs on first launch and can be re-run.

### 7.5 Known technical debt

Accepted shortcuts to revisit; each names its trigger.

| Item | Why it is accepted now | Revisit when |
| --- | --- | --- |
| Tests use `httpx` with `starlette.testclient`, which Starlette deprecates in favour of `httpx2` (a `StarletteDeprecationWarning` in every test run). | Works with the versions locked in `uv.lock`; `httpx` stays anyway as the M4 fetch dependency; switching adds a dev dependency (user decision, 2026-10-03). | A Starlette or FastAPI upgrade drops `httpx` support in `TestClient`, or before M9 release hardening. |
