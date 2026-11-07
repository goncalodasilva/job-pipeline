# Job Search Pipeline

A local, open-source web app for an honest, organized job search. You collect job postings, see how well your own experience fits each one, build a bank of confirmed facts about your work, draft tailored CVs and cover letters from those facts, track every application on a board and prepare for interviews. The app runs on your computer and opens in your browser. Its AI features use Claude, through the Claude Agent SDK.

What it is not:

- It never submits an application. You apply on the employer's site yourself, then mark the application Applied.
- It is not an online service. There is no app account, sync or cloud copy. Your data stays in a folder on your computer, and the app only listens on `127.0.0.1`.
- It doesn't search job boards for you. You add each job by its link or by pasting its text.

**Status:** version 1.0.0. Supported on macOS and Linux. Windows is not supported. The interface is in English.

## What you need

- macOS or Linux, with Python 3.11 or later (3.11 to 3.14 are supported).
- [uv](https://docs.astral.sh/uv/) and git.
- For the AI features, one of these:
  - a paid Claude plan that includes Claude Code, such as Pro or Max. To make a sign-in token you need Claude Code installed;
  - an Anthropic API key from Claude Console.

The demo needs neither.

## Install

```bash
git clone https://github.com/goncalodasilva/job-pipeline.git
cd job-pipeline
uv sync
```

## Run

From the project folder:

```bash
uv run jobpipeline
```

The app serves on http://127.0.0.1:8000 and opens your browser when it is ready. On the first run, the setup guide opens. Stop the app with Ctrl+C. Everything you saved stays in the data folder, so you carry on where you left off next time.

### Options

| Option | What it does |
| --- | --- |
| `--port N` | Use port N instead of 8000 |
| `--data-dir PATH` | Use this folder for your data, even with `--demo` |
| `--no-browser` | Don't open the browser; go to the address yourself |
| `--setup` | Open the setup guide again, to check or change what you set up |
| `--demo` | Use the separate demo workspace (see [Try the demo](#try-the-demo)) |
| `--version` | Show the version and exit |
| `-h`, `--help` | Show the options and exit |

### Environment variables

You don't need any of these. They cover special cases.

| Variable | What it does |
| --- | --- |
| `JOBPIPELINE_DATA_DIR` | Data folder. `--data-dir` wins over it; it wins over `--demo` and the folder chosen in setup |
| `JOBPIPELINE_CONFIG_DIR` | Folder for the setup file, `config.toml`, which records where your data folder is. Meant for tests. If it points at a folder with no setup file, **Delete all data** can't see your usual setup file and removes the saved keys that every workspace shares |
| `JOBPIPELINE_PORT` | Port to use when `--port` isn't given |
| `JOBPIPELINE_LOG_LEVEL` | `DEBUG`, `INFO` (the default), `WARNING` or `ERROR` |
| `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` | Your API key or sign-in token, when no keychain can be used (see [Credentials and the keychain](#credentials-and-the-keychain)) |
| `JOBPIPELINE_ALLOW_FAKE_PROVIDERS` | `1` offers the practice provider outside the demo folder. For testing only |

## Try the demo

```bash
uv run jobpipeline --demo
```

The demo opens straight into a fictional workspace, so you can look around before you set anything up. It belongs to Alex Meadow (Fictional): fifteen jobs across every inbox state, nine companies, six applications from Preparing to Withdrawn, two interviews (one of them today) and a small fact bank. Dates are moved to the day the demo is filled, so the data looks current.

- The demo has its own data folder (`jobpipeline-demo`, next to the usual one) and its own keychain service. It never reads or writes your real data folder or setup file, and it ignores credentials in environment variables.
- AI features use the practice provider, listed as "Fake (testing only)". It needs no account and calls no AI service: it returns fixed fictional text, labelled as practice data (for example "Practice data (fake provider)"). Two Claude options are listed too. If you choose one and save a token or key, the demo calls Claude.
- To reset the demo, use Settings > Data and privacy > **Delete all data…** inside it. That empties the demo, and the next `uv run jobpipeline --demo` start fills it again. First check that the page says "Demo workspace": with `--data-dir` or `JOBPIPELINE_DATA_DIR` set, `--demo` opens that folder instead, and Delete all data would empty it.
- With `--data-dir` or `JOBPIPELINE_DATA_DIR`, that folder is used instead. Nothing is filled in, and the practice provider is offered only when `JOBPIPELINE_ALLOW_FAKE_PROVIDERS=1` is set.

## First run: the setup guide

1. **Welcome and data folder.** Choose where the app keeps your data, settings and logs. A folder is suggested: `~/Library/Application Support/jobpipeline` on macOS, usually `~/.local/share/jobpipeline` on Linux. This step also offers **Restore from an export** and explains how to start the demo.
2. **AI provider.** Choose how to connect to Claude:
   - **Claude subscription (sign-in token):** with Claude Code installed, run `claude setup-token` in a terminal, sign in, then paste the token it shows.
   - **Claude Code sign-in on this computer:** uses your existing Claude Code sign-in. There's nothing to paste.
   - **Anthropic API key:** create a key in Claude Console and paste it. Requests use your Console account's API credit, separately from any Claude plan.

   A token or key goes to your system keychain and is never shown again. **Test connection** sends one very small request, which uses a tiny amount of your plan or credit. Anthropic's Agent SDK documentation asks developers who build products or services on the SDK to use API key authentication, not a Claude plan sign-in, unless Anthropic has approved it. Read the [Agent SDK overview](https://docs.claude.com/en/docs/agent-sdk/overview) and decide which option fits you.
3. **Web search (optional).** Searches go through your AI provider (with Claude, its built-in web search) and run only when you press a web-search button. You can set a monthly budget, continue without one, or choose **Skip for now**.
4. **Import CV (optional).** Upload a PDF, Word (.docx) or plain-text file of up to 10 MB, or paste the text. What the CV states becomes drafts that you check in the Fact bank. Nothing is confirmed for you.
5. **Job preferences.** The roles you're looking for, locations, work modes, minimum salary, must-haves, deal-breakers and the country your CV is for.
6. **Done.** A summary of what you set up and what you skipped, then **Add a job** or **Go to Today**.

Run the guide again with `--setup`, or from Settings > Providers and AI > **Re-run the setup guide**. Nothing is reset.

## What stays on your computer, and what is sent

- Everything you add stays in the data folder on this computer. Settings > Data and privacy shows where it is.
- Only the text a request needs goes to the AI provider you chose: for example the job text to summarize a job, your CV's text (with any contact details in it) to import it, or your confirmed facts to tailor a CV. Facts marked "Never use in applications" are never sent.
- The name, contact details and photo you save in Settings are never sent to the AI.
- Web searches run only when you press a web-search button. They send only details such as the company's name, website and industry and the role's title, never your facts or personal details.
- AI output is a suggestion for you to review. Nothing the AI writes about you becomes a fact, a CV or a letter until you confirm or approve it. Tailored CVs, letters and answers are built only from facts you confirmed, and the app flags what it can't match to them. The checks help, but they can't catch everything: read every word.

## Credentials and the keychain

The app keeps your token or API key in the system keychain: Keychain on macOS, and a Secret Service keyring (such as GNOME Keyring) on Linux. It never writes them to the data folder, logs or exports. On macOS, if your computer asks to allow access when you save, choose Allow.

If no keychain can be used, give the app your key through a variable each time you start it. `read -rs` reads the key without showing it, so it stays out of your shell history. Run the first line, paste the key and press Enter. The second line starts the app with the key set for the app alone, not for later commands in the same terminal:

```bash
read -rs key
ANTHROPIC_API_KEY="$key" uv run jobpipeline
unset key
```

For a subscription token, use `CLAUDE_CODE_OAUTH_TOKEN="$key"` instead. Then, in the AI provider step, choose the matching option and save it with the field empty. A key saved in the keychain wins over the variable. The app reads these variables but never changes them. The demo never reads them.

## Backing up and restoring

**Export.** Settings > Data and privacy > **Export** saves one JSON file with everything in the workspace: your data, documents, photo, CV uploads and prompt changes. Saved keys, backups, logs and earlier exports are not included. Usage snapshots are added only if you tick **Include provider usage snapshots**. The file goes to the data folder's `exports` folder; press **Download** to keep a copy elsewhere. It isn't encrypted, so keep it private.

**Restore.** A restore goes only into an empty workspace; nothing is merged. On a new computer, or after deleting all data, use setup step 1: under **Restore from an export**, enter a new or empty data folder, choose the file and press **Restore from this file**. Settings > Data and privacy > **Import** does the same while the open workspace has no content.

An export from an older version is brought up to date; one from a newer version is refused. Keys are never exported, so after a restore the app may ask you to fix the AI provider: open that step and save your sign-in again.

## Deleting all data

Settings > Data and privacy > **Delete all data…** opens a confirmation. Type `delete` in lowercase, then press **Delete all data**. The setup guide then starts again.

- It removes everything inside the app's folders: your data, documents, imports, assets, prompts, backups, exports and logs. It also removes the keys the app saved in your keychain and the setup file.
- Saved keys are shared by every workspace on this computer (the demo has its own). If the setup file points at another data folder, the keys and the setup file are kept.
- A key you set as an environment variable can't be removed by the app. Remove it from your environment yourself.
- With `JOBPIPELINE_CONFIG_DIR` pointing at a folder that has no setup file, the app can't see your usual setup file, so it treats the saved keys as this workspace's and removes them.
- Files the app didn't create are left in place, and the dialog lists them. Deleted files are not securely overwritten, so backups such as Time Machine may keep copies.
- **Export first** if you want to keep your work, and download the file: exports inside the data folder are deleted too.

## PDF and Word export

Tailored CVs and cover letters export as PDF and Word files from step 5 of an application's Tailoring tab. PDF export uses the `typst` Python package, which `uv sync` installs as one self-contained wheel: there are no system libraries to install on macOS or Linux, and it needs no network access. Fonts come from the package, not from your computer, so a PDF looks the same on both systems.

If `typst` can't be loaded, PDF export says "PDF export needs typst. Word export still works; see the README." Word export doesn't depend on it, and the app starts either way. Run `uv sync` again to repair the install.

## Updating

```bash
git pull
uv sync
```

On the next start, the app copies your database into the data folder's `backups` folder before it changes the database's structure. It keeps the last five copies.

## Keyboard shortcuts

| Keys | What they do |
| --- | --- |
| Ctrl+K (Cmd+K on a Mac) | Command palette: search and actions |
| G then T, I, B or F | Go to Today, Inbox, Board or Fact bank |
| N | Quick add: a job, a fact or an event |
| ? | Show every shortcut |
| Esc | Close a drawer or dialog |
| J, K, A, R, Enter, Z | In the Job inbox: next, previous, apply, remove, open details, undo |

Single-key shortcuts are ignored while you type. Turn them off in Settings > Keyboard shortcuts if they clash with speech input or assistive technology; Ctrl+K (Cmd+K on a Mac) and Esc always work. At the foot of every page except the setup guide, the **Keyboard shortcuts** button shows the keys you can use (and how to turn the single keys back on), and **Quick add** adds a job, a fact or an event. The [user guide](docs/user-guide.md) has the details, and its [Accessibility](docs/user-guide.md#accessibility) section covers screen readers, zoom and known limits.

## Troubleshooting

- **"Port 8000 is already in use."** The message suggests another port. Start with that one, for example `uv run jobpipeline --port 8010`.
- **"This folder is already open in another copy of the app."** Only one copy can use a data folder. Stop the other copy (Ctrl+C in its terminal), or choose another folder with `--data-dir`.
- **"The data folder … belongs to another user."** The app refuses a folder owned by another account. Choose a folder you own. Don't run `jobpipeline` with `sudo`: a folder it creates then belongs to root.
- **Keychain or sign-in problems.** If the keychain is locked, unlock it and try again, or use an environment variable (see [Credentials and the keychain](#credentials-and-the-keychain)). On Linux, install a Secret Service keyring such as GNOME Keyring. A rejected or expired token: run `claude setup-token` again and paste the new one. A banner that says **Needs attention** links to **Fix the AI provider**. "Claude Code wasn't found": run `uv sync` again.
- **PDF export is unavailable.** Run `uv sync` again (see [PDF and Word export](#pdf-and-word-export)).
- **"The app was restarted… Reload the page"** appears when the app restarted while a page was open. Reload the page.
- **Logs.** They are in the `logs` folder of your data folder (`jobpipeline.log`). If the app can't start, the error says where to look. Keys and tokens never appear in logs.
- **Reopen the setup guide:** `uv run jobpipeline --setup`.
- **Reset the demo:** check that the page says "Demo workspace", then Delete all data inside the demo, and start `uv run jobpipeline --demo` again.
- **Look around without touching your data:** use the demo (`uv run jobpipeline --demo`). A throwaway workspace, `uv run jobpipeline --data-dir "$(mktemp -d)"`, also leaves your usual data folder alone, with two catches: saved keys are shared by every workspace on this computer, so a key you save there replaces the one your usual workspace uses; and on a computer where the app was never set up, finishing setup there records the temporary folder in the setup file, so run `uv run jobpipeline --setup` afterwards to choose your real folder.
- **`No module named 'app'` on macOS.** macOS can mark the virtual environment's `.pth` files as hidden, and newer Python versions skip hidden `.pth` files. Delete `.venv` and run `uv sync` again, or run `chflags nohidden .venv/lib/python3.*/site-packages/*.pth`. `uv run python -m app` from the project folder works either way.
- **Keep the project out of iCloud-synced folders** such as `~/Documents` or `~/Desktop` when "Desktop & Documents Folders" sync is on. iCloud can evict files inside `.venv`, which breaks imports in confusing ways. Move the project (for example to `~/code/job-pipeline`), then run `uv sync`.

## Documentation

- [User guide](docs/user-guide.md): how to use each part of the app, day to day.
- [Detailed design](docs/references/detailed-design.md): screens, interactions and UX rules.
- [Technical specification](docs/TECHNICAL_SPEC.md): architecture, rules, tests and roadmap.
- [Decisions](docs/DECISIONS.md): what was decided, and why.
- [Developing with Claude Code and Codex](docs/development/agents.md): setup, commands, permissions and handoffs.

## Developing with Claude Code and Codex

Both coding tools share `AGENTS.md`, the engineering rules, the development procedures, verification and the progress records in `docs/PROGRESS.md`. Claude Code keeps its slash commands and specialist agents; Codex uses repository skills and independent reviewer profiles. See [docs/development/agents.md](docs/development/agents.md) and technical spec section 6.

The workflow is plan, then one task or a milestone run, then acceptance. Claude's entry points are `/plan-milestone`, `/next-task`, `/run-milestone`, `/verify` and `/milestone-acceptance`; Codex uses the same names as skills (`$next-task M3.8`, for example). Review and trust each client's project hooks before relying on them. Use one writer per checkout.

| Purpose | Command |
| --- | --- |
| Install (once per clone) | `uv sync`, then `uv run pre-commit install` |
| Browsers for end-to-end tests (once) | `uv run playwright install chromium` |
| Format and lint | `uv run ruff format .` and `uv run ruff check .` |
| Types | `uv run mypy` |
| Module boundaries | `uv run lint-imports` |
| Fast tests | `uv run pytest -m "not e2e and not smoke and not live"` |
| Smoke and end-to-end tests | `uv run pytest -m smoke` and `uv run pytest -m e2e` |
| Quick verification | `bash scripts/verify.sh quick` |
| Full verification (adds end-to-end tests, coverage gates, the wheel check and all hooks) | `bash scripts/verify.sh full` |
| Wheel contents (builds the wheel and checks what it holds) | `uv run python scripts/check_wheel.py` |
| PDF renderer check on Linux (needs Docker) | `bash scripts/linux_render_check.sh` |

Live tests call real services and are opt-in: they need both `JOBPIPELINE_LIVE_TESTS=1` and `-m live`. See `docs/live-check-m1.md`. They read credentials only from `JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN` and, optionally, `JOBPIPELINE_LIVE_ANTHROPIC_API_KEY`, never from the keychain or `.env`. Read them without echoing: run the first line, paste the token from `claude setup-token` and press Enter.

```bash
read -rs token
JOBPIPELINE_LIVE_CLAUDE_OAUTH_TOKEN="$token" JOBPIPELINE_LIVE_TESTS=1 uv run pytest -m live
unset token
```

Don't add `-l`/`--showlocals`, `-vv` or `--junitxml` to a live run: they can print or store values from a failing call.

CI (GitHub Actions) runs lint, types, module boundaries, a secrets scan, fast, smoke, end-to-end, wheel and coverage jobs on Ubuntu for every push. macOS runs fast and smoke tests on pull requests to main, on manual dispatch and weekly (ADR-073).

## License

MIT. See [LICENSE](LICENSE).
