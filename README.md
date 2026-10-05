# Job Search Pipeline

A local, open-source web app for running a job search: collect postings, see honestly how well you fit, position yourself for each role with a growing bank of your own facts, draft tailored CVs and cover letters, track every application on a board, and prepare for interviews. You always submit applications yourself.

**Status:** milestone M0 (foundation: app shell, security guards, tooling) is nearly complete; the features arrive from M1 on. See `docs/PROGRESS.md`.

## How it works

- Runs on your computer and opens in your browser. Your data stays in a local data folder.
- Uses Claude through the Claude Agent SDK, with your Claude subscription or an API key. CV and job data is sent to the AI provider you configure.
- Every AI output is a draft you review. Tailored documents can only use facts you confirmed.

## Quick start (once milestone M1 is done)

```bash
git clone <repository-url> job-pipeline
cd job-pipeline
uv sync
uv run jobpipeline
```

The setup guide opens on first launch and walks you through choosing a data folder, connecting Claude, and setting your job preferences. Full instructions and troubleshooting: `docs/references/detailed-design.md`, section "Installing and running the app".

Using a Claude subscription draws from your plan's usage limits, and Anthropic's terms ask developers building products or services with the Agent SDK to use an API key. Check Anthropic's current terms for your situation.

## Running the app today

You need [uv](https://docs.astral.sh/uv/). The app runs on Python 3.11 to 3.14.

```bash
uv sync
uv run jobpipeline
```

The app serves on http://127.0.0.1:8000 and opens your browser once it is ready. Stop it with Ctrl+C. Options:

| Option | Meaning |
| --- | --- |
| `--data-dir PATH` | Folder for your data (default: your system's app-data folder) |
| `--port N` | Port to use (default 8000) |
| `--no-browser` | Don't open the browser |
| `--demo` | Use a separate demo workspace |
| `--setup` | Open the setup guide again (from M1) |
| `--version` | Show the version |

To look around without touching your real data folder, use a fresh temporary one, for example on macOS or Linux `uv run jobpipeline --data-dir "$(mktemp -d)"`. Don't put real data in it.

## Troubleshooting

- **`No module named 'app'` when running `jobpipeline` on macOS.** macOS can mark the virtual environment's `.pth` files as hidden, and newer Python versions skip hidden `.pth` files. Delete `.venv` and run `uv sync` again, or unhide uv's own files with `chflags nohidden .venv/lib/python3.*/site-packages/*.pth`. Running `uv run python -m app` from the project folder works either way.
- **Keep the project out of iCloud-synced folders** such as `~/Documents` or `~/Desktop` when "Desktop & Documents Folders" sync is on. iCloud can flag or evict files inside `.venv`, which breaks imports in confusing ways. Move the project elsewhere (for example `~/code/job-pipeline`), or recreate `.venv` with `uv sync`.
- **"The data folder … belongs to another user."** On macOS and Linux the app refuses a data folder owned by another account, since that account could replace or redirect your files. Choose a folder you own, for example the default one, or a new folder with `--data-dir`. Don't run `jobpipeline` with `sudo`: a folder it creates as root then belongs to another user.
- **Port already in use.** Another program uses port 8000. Start with `--port 8123` (any free port).
- **"The app was restarted… Reload the page"** appears when the app restarted while the page was open. Reload the page.

## Documentation

- `docs/TECHNICAL_SPEC.md`: architecture, rules, testing strategy, roadmap, and how the app is built with Claude Code
- `docs/PROGRESS.md`: what is done and what is next
- `docs/DECISIONS.md`: decisions and their reasons
- `docs/references/`: product spec and detailed design

## Developing with Claude Code

This repository includes a complete Claude Code setup: `CLAUDE.md`, subagents in `.claude/agents/`, skills in `.claude/skills/`, rules in `.claude/rules/`, and hooks in `.claude/hooks/`. The loop is `/plan-milestone M<n>`, then `/next-task` (one task per run), then `/milestone-acceptance M<n>`. Details in `docs/TECHNICAL_SPEC.md` section 6.

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
| Quick verification | `bash .claude/skills/verify/scripts/verify.sh quick` |
| Full verification (adds e2e, coverage gates, all hooks) | `bash .claude/skills/verify/scripts/verify.sh full` |

Live tests that call real services are opt-in: `JOBPIPELINE_LIVE_TESTS=1 uv run pytest -m live`. CI (GitHub Actions) runs lint, fast, smoke, end-to-end and coverage jobs on Ubuntu for every push, and fast and smoke tests on macOS for pull requests to `main` (ADR-032). Windows is not supported: its development is suspended (ADR-061).

## License

MIT. See `LICENSE`.
