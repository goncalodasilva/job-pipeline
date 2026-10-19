# Job Search Pipeline

A local, open-source web app for running a job search: collect postings, see honestly how well you fit, position yourself for each role with a growing bank of your own facts, draft tailored CVs and cover letters, track every application on a board, and prepare for interviews. You always submit applications yourself.

**Status:** M0–M2 are accepted. M3 adds the fact bank, CV import and Refine with AI; acceptance is pending. Job intake and application tracking arrive from M4 on. See `docs/PROGRESS.md`.

## How it works

- Runs on your computer and opens in your browser. Your data stays in a local data folder.
- Uses Claude through the Claude Agent SDK, with your Claude subscription or an API key. CV and job data is sent to the AI provider you configure.
- Every AI output is a draft you review. Tailored documents can only use facts you confirmed.

## Getting started

**Supported platforms:** macOS and Linux. Python 3.11 or later.

```bash
git clone <repository-url> job-pipeline
cd job-pipeline
uv sync
uv run jobpipeline
```

The setup guide opens on first launch. Follow these steps:

1. **Choose a data folder:** the app saves your settings, data and logs there. A folder is suggested for your system; you can choose a different one.

2. **Connect Claude:** choose how you authenticate:
   - **Claude subscription (sign-in token):** run `claude setup-token` in the terminal, paste the token here.
   - **Claude Code sign-in on this computer:** the app uses your existing Claude Code login.
   - **Anthropic API key:** create one in the Claude Console, paste it here.
   
   The setup guide tests your connection with a tiny request (using a negligible amount of your plan or credits). Anthropic's Agent SDK documentation asks developers who build products or services on the SDK to use API key authentication, not a Claude plan sign-in, unless Anthropic has approved it. See https://docs.claude.com/en/docs/agent-sdk/overview for details.

3. **Web search (optional):** nothing in this version searches the web yet, and web search only runs when you ask for it. You can set a monthly limit on web searches now. Leave the budget empty for no limit, or choose "Skip for now".

4. **Import CV (optional):** upload a PDF, Word document or UTF-8 text file, or paste text. Everything extracted starts as a draft to review in the Fact bank. You can skip this and return later.

5. **Set your job preferences:** target roles, locations, work preferences, preferred salary and your CV country. You can edit these anytime by re-running the setup guide.

6. **Done:** the app opens with a choice to add your first job or go to Today. Skipped optional steps have links here. You can re-run the setup guide anytime with `--setup`.

In Settings, you can change your AI connection, replace a saved credential, and choose which models to use for fast and thorough tasks.

Troubleshooting and more: see the sections below and `docs/references/detailed-design.md`.

## Running the app

You need [uv](https://docs.astral.sh/uv/). The app runs on Python 3.11 or later.

```bash
uv sync
uv run jobpipeline
```

The app serves on http://127.0.0.1:8000 and opens your browser once it is ready. Stop it with Ctrl+C.

### Options

| Option | Meaning |
| --- | --- |
| `--data-dir PATH` | Folder for your data (default: your system's app-data folder) |
| `--port N` | Port to use (default 8000) |
| `--no-browser` | Don't open the browser |
| `--demo` | Use a separate demo workspace with its own data folder and keychain service; it offers the practice provider on that demo folder only, not with `--data-dir` (see below) |
| `--setup` | Open the setup guide to change settings or credentials |
| `--version` | Show the version |

To explore without using your real data folder, try the demo:

```bash
uv run jobpipeline --demo
```

The demo's second setup step offers the practice provider, listed as "Fake (testing only)". It needs no account or key. Choose it, then start practice tasks from Settings > Testing tools. A practice task makes five fake calls and sends nothing to any service. Only the practice provider is fake: the Claude options are still listed in the demo, and if you choose one and save your own token or key, the app calls Claude.

Or use a fresh temporary folder:

```bash
uv run jobpipeline --data-dir "$(mktemp -d)"
```

## Background tasks

AI work runs as background tasks. The **Tasks** button in the top bar shows how many are running or queued, and says "1 needs attention" when a failed or interrupted task is waiting for you to look at it. Open it to see each task's progress and its buttons:

- **Cancel** stops a queued or running task. A running task that is cancelled discards its partial output.
- **Retry** runs a failed, cancelled or interrupted task again.
- If the app stopped while a task ran, for example after a crash, that task shows as **interrupted** when the app starts again. It runs again only when you press Retry. Queued tasks run after a restart.

Failed and interrupted tasks retry only when you ask. CV import and Refine with AI use the runner; invalid AI output gets one automatic correction attempt. The practice task is offered only on the demo's own folder, or when `JOBPIPELINE_ALLOW_FAKE_PROVIDERS=1` is set. With `--data-dir` or `JOBPIPELINE_DATA_DIR`, even `--demo` needs that variable.

## Fact bank and CV import

Open **Fact bank > Drafts to review** to import a CV (up to 10 MB and 60,000 extracted characters). Scans need pasted text; there is no OCR. Review each draft beside its source quote, then confirm, edit and confirm, merge into a confirmed fact, or discard with undo. Batch confirmation leaves drafts marked “Needs check” for individual review. A failed import keeps its upload with Start and Remove actions. Removing an upload keeps facts already extracted from it.

The **Library** searches current wording, roles and skills, with role, skill and status filters. Edits create versions with a history and word diff. The **Capture inbox** keeps quick notes you can turn into facts. “Find possible duplicates” uses text similarity on demand.

In a fact's editor, **Refine with AI** asks questions and offers a diff. Nothing changes until you press Accept. Claims the checker cannot support must be ticked as true or edited out; a ticked claim stays in that fact's wording. Review the whole suggestion: the checker cannot catch every embellishment. “Never use in applications” excludes a fact from AI requests and blocks refinement. “Generalize names” instructs the AI to hide names in its output; the provider still receives the source text. Refinement also sends other usable confirmed facts and shows how many were used.

The fake provider makes fixed fictional suggestions and labels them **Practice data (fake provider)**. It does not understand the CV or fact you give it. Use fictional text when practicing. Fact versions and closed conversations remain in the local database; discard and archive do not erase their history.

## Usage budgets

Settings > Usage budgets sets a monthly limit for each service: Claude subscription, Claude API key, Web search and, in the demo, the practice provider. A service has no limit until you set one. A budget has an allowance, a first and a second warning, a blocking point (at most the allowance), a reset day, and optional caps for the Fast and Thorough tiers. Days 29 to 31 mean the last day of shorter months. Limits count requests; token counts are shown but not used for limits yet.

Each warning appears once per cycle, in a dialog that asks you to continue. At the blocking point the action is disabled and says when the count resets. The usage panel above the forms shows each service's count, its reset date and any calls still running. Claude's own plan usage is shown for information only and never blocks an action. A connection test counts as one request and is never blocked.

Connection tests, CV import, Refine with AI and practice calls count toward usage, including failed calls and AI correction attempts. Web search budgets take effect once web search is used. "Background tasks at once" sets how many tasks run together; a change applies the next time you start the app.

## Credentials and the keychain

The app stores your Claude credential in the OS keychain so it is never written to files or logs.

**On macOS:** uses Keychain. When you save a credential in the app, if your computer asks to allow access, choose Allow.

**On Linux:** uses Secret Service (the freedesktop.org standard). If your keyring is locked, the app shows a message and you can use environment variables instead (see below).

**Without a keychain:** if the OS keychain is unavailable or locked, set one of these environment variables in the shell that starts the app. Use `read -rs` to keep them out of your shell history:

```bash
# For API key, read it without echoing:
read -rs ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY
uv run jobpipeline

# Or for subscription token:
read -rs CLAUDE_CODE_OAUTH_TOKEN
export CLAUDE_CODE_OAUTH_TOKEN
uv run jobpipeline
```

Then on the AI provider step, choose the matching option (subscription token or API key) and save it with the field empty. A saved keychain item wins over the environment variable. The app never writes these variables anywhere.

**Demo mode** uses a separate keychain service and never reads environment credentials, so demo runs never see your real credentials.

## Troubleshooting

- **`No module named 'app'` when running `jobpipeline` on macOS.** macOS can mark the virtual environment's `.pth` files as hidden, and newer Python versions skip hidden `.pth` files. Delete `.venv` and run `uv sync` again, or unhide uv's own files with `chflags nohidden .venv/lib/python3.*/site-packages/*.pth`. Running `uv run python -m app` from the project folder works either way.

- **Keep the project out of iCloud-synced folders** such as `~/Documents` or `~/Desktop` when "Desktop & Documents Folders" sync is on. iCloud can flag or evict files inside `.venv`, which breaks imports in confusing ways. Move the project elsewhere (for example `~/code/job-pipeline`), or recreate `.venv` with `uv sync`.

- **"The data folder … belongs to another user."** On macOS and Linux the app refuses a data folder owned by another account, since that account could replace or redirect your files. Choose a folder you own, for example the default one, or a new folder with `--data-dir`. Don't run `jobpipeline` with `sudo`: a folder it creates as root then belongs to another user.

- **Port already in use.** Another program uses port 8000. Start with `--port 8123` (any free port).

- **"The app was restarted… Reload the page"** appears when the app restarted while the page was open. Reload the page.

## Documentation

- `docs/TECHNICAL_SPEC.md`: architecture, rules, testing strategy, roadmap, and development with Claude Code and Codex
- `docs/PROGRESS.md`: what is done and what is next
- `docs/DECISIONS.md`: decisions and their reasons
- `docs/references/`: product spec and detailed design

## Developing with Claude Code and Codex

Both tools share `AGENTS.md`, engineering rules, development procedures, verification and progress records. Claude Code keeps its slash commands and specialists; Codex uses repository skills and independent reviewer profiles. See [setup, commands, permissions and handoffs](docs/development/agents.md) and technical spec section 6.

The workflow is plan → one task or a milestone run → acceptance. Claude entry points are `/plan-milestone`, `/next-task`, `/run-milestone`, `/verify`, `/milestone-acceptance`; Codex uses the same names as skills (`$next-task M3.8`, for example). Trust/review project hooks in each client before relying on them. Use one writer per checkout.

Developer commands (spec 4.5):

| Purpose | Command |
| --- | --- |
| Install (once per clone) | `uv sync`, then `uv run pre-commit install` |
| Browsers for end-to-end tests (once) | `uv run playwright install chromium` |
| Format and lint | `uv run ruff format .` and `uv run ruff check .` |
| Types (app and scripts) | `uv run mypy` |
| Module boundaries | `uv run lint-imports` |
| Fast tests | `uv run pytest -m "not e2e and not smoke and not live"` |
| Smoke and end-to-end tests | `uv run pytest -m smoke` and `uv run pytest -m e2e` |
| Quick verification | `bash scripts/verify.sh quick` |
| Full verification (adds e2e, coverage gates, all hooks) | `bash scripts/verify.sh full` |

Live tests that call real services are opt-in. See `docs/live-check-m1.md` for the full checklist. Both the environment variable and the marker are required: set `JOBPIPELINE_LIVE_TESTS=1` and run with `-m live`. Read credentials into shell variables without echoing them (using `read -rs`) so they stay out of shell history:

```bash
read -rs JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN  # paste the token from `claude setup-token`, then Enter
export JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN
JOBPIPELINE_LIVE_TESTS=1 uv run pytest -m live
unset JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN
```

Don't add `-l`/`--showlocals`, `-vv` or `--junitxml` to a live run: they can print or store local values from a failing call. Live tests read credentials only from `JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN` and `JOBPIPELINE_LIVE_ANTHROPIC_API_KEY` (optional), never from the keychain or .env.

CI (GitHub Actions) runs lint, types, module boundaries, secrets scan, fast, smoke, end-to-end and coverage jobs. Ubuntu runs on every push to any branch; macOS runs on pull requests to main, manual dispatch and the weekly schedule (ADR-073). Windows development is suspended (ADR-061).

## License

MIT. See `LICENSE`.
