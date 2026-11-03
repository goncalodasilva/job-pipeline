# Job Search Pipeline

A local, open-source web app for running a job search: collect postings, see honestly how well you fit, position yourself for each role with a growing bank of your own facts, draft tailored CVs and cover letters, track every application on a board, and prepare for interviews. You always submit applications yourself.

**Status:** M0–M5 are accepted. The Fact bank, CV import, Refine with AI, job intake, Job inbox, Companies, application tracking, contacts, calendar files, in-app reminders and Today are available. M6 (tailoring, the honesty checks, your details, CV templates and PDF and Word exports) is in progress: its features are built, and its acceptance checks have not been run. The user deferred M5's unperformed manual checks to M6 acceptance; see `docs/PROGRESS.md` and `docs/acceptance/M6.md`.

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

Failed and interrupted tasks retry only when you ask. CV import, Refine with AI, job summaries, fit assessments, session questions and session answers use the runner (fetching a job page does not: it happens while you wait); invalid AI output gets one automatic correction attempt. The practice task is offered only on the demo's own folder, or when `JOBPIPELINE_ALLOW_FAKE_PROVIDERS=1` is set. With `--data-dir` or `JOBPIPELINE_DATA_DIR`, even `--demo` needs that variable.

## PDF and Word export

Tailored documents export as Word files and as PDFs. PDF export uses the `typst` Python package, which `uv sync` installs as one self-contained wheel: there are no system libraries to install on macOS or Linux, and it needs no network access. Fonts come from the package, not from your computer, so a PDF looks the same on both systems.

If `typst` can't be loaded, PDF export says "PDF export needs typst. Word export still works; see the README." Word export doesn't depend on it, and the app starts either way. Run `uv sync` again to repair the install.

Developers can check the renderers on Linux from any OS with Docker running: `bash scripts/linux_render_check.sh` runs only the renderer tests in a `python:3.11-slim-bookworm` container (it pulls the image and downloads the locked packages inside the container, and prints `LINUX RENDER CHECK: PASS`). It is not part of `verify.sh` or CI.

Exports are made only when you press **Export files** in step 5 of the Tailoring tab. They use the approved CV and, when it is approved, the approved cover letter (a letter that is not approved is left out). Files are written to a folder for the application inside your data folder, `documents/<application id>/`, which only your user account can read. Names look like `cv-v2-<24 characters>.pdf` or `cover-letter-v1-<24 characters>.docx`, where the number is the version. Download each file from step 5. The app never sends them anywhere. An export keeps the name and contact details that were current when that version was first exported; a later change to your details does not rewrite it. If a format is missing for a version, approve a new version of that document to export it.

When you mark the application Applied, the app records the latest exported approved version of the CV and of the letter (or the latest approved version, if none was exported) as sent. Their files never change. Later edits make new versions, which you export as new files.

## Your details and photo

Open **Settings > Your details** to save your name, email, phone, location and up to three links (http or https). Your name is needed to export a CV or letter; the other fields are optional. The details appear in the header of a CV and in the sign-off of a letter. They are never sent to the AI. The app does not store or ask for a date of birth, nationality or marital status.

You can add one photo: a JPEG or PNG of at most 5 MB, between 100 and 4000 pixels on each side. The app removes hidden data from the file, such as where and on what it was taken and any text notes, and keeps the photo in the `assets` folder of your data folder under a generated name, readable only by your user account. The photo appears on a CV only when that CV's template has a place for one and you have uploaded a photo. Replacing or removing the photo deletes the old file.

## CV templates

A country's CV template sets the sections and their order, optional heading labels, one to three pages, whether a photo place is shown, which contact details appear, the date format and the page size. Press **Draft a template for <country>** under **Settings > CV templates** (or the **Draft the template** link in step 3 of the Tailoring tab, shown while no template is ready), and the AI proposes one from the country, your style choices and an optional short note. No facts and no personal details are sent for this. Review every field, then press **Approve template**. Other applications in the same country reuse the approved template without a new request. Editing an approved template returns it to draft until you approve it again.

## Tailoring a CV and cover letter

Each application has a **Tailoring** tab with five steps. Every AI step starts only when you press its button, counts toward your usage budget, and sends one request (plus at most one automatic correction if the answer is malformed).

1. **Gap check.** The AI compares the job's requirements with your confirmed facts. A requirement counts as shown only when the facts you confirmed show it. If must-have requirements are missing, the app says that the role may not be worth applying for. You can add a fact at a gap, or check again.
2. **Angle.** The AI proposes two or three angles for the application. Pick one, edit it, or write your own.
3. **CV.** A draft CV is built only from confirmed facts. Each line is marked Added, Removed, Reworded or Moved against your master CV. A line the checker cannot support (a number, employer or skill that your facts do not state) is flagged. Edit, remove or confirm each flag, and **Approve CV** stays disabled until nothing is flagged. Read every word: the check helps, but it cannot catch every embellishment.
4. **Cover letter.** Optional. The greeting, opening line, closing and your name come from the app. The body paragraphs are drafted from the same confirmed facts, and you approve them with **Approve letter**. Submit says "No cover letter" if you skip it.
5. **Submit.** A checklist, the export of PDF and Word files, and the button **I've submitted it — mark Applied**. The app never submits an application for you.

With the fake provider, every draft is labelled as practice data and uses fictional text.

## Fact bank and CV import

Open **Fact bank > Drafts to review** to import a CV (up to 10 MB and 60,000 extracted characters). Scans need pasted text; there is no OCR. Review each draft beside its source quote, then confirm, edit and confirm, merge into a confirmed fact, or discard with undo. Batch confirmation leaves drafts marked “Needs check” for individual review. A failed import keeps its upload with Start and Remove actions. Removing an upload keeps facts already extracted from it.

The **Library** searches current wording, roles and skills, with role, skill and status filters. Edits create versions with a history and word diff. The **Capture inbox** keeps quick notes you can turn into facts. “Find possible duplicates” uses text similarity on demand.

In a fact's editor, **Refine with AI** asks questions and offers a diff. Nothing changes until you press Accept. Claims the checker cannot support must be ticked as true or edited out; a ticked claim stays in that fact's wording. Review the whole suggestion: the checker cannot catch every embellishment. “Never use in applications” excludes a fact from AI requests and blocks refinement. “Generalize names” instructs the AI to hide names in its output; the provider still receives the source text. Refinement also sends other usable confirmed facts and shows how many were used.

The fake provider makes fixed fictional suggestions and labels them **Practice data (fake provider)**. It does not understand the CV or fact you give it. Use fictional text when practicing. Fact versions and closed conversations remain in the local database; discard and archive do not erase their history.

## Enrichment sessions and coverage

A **session** is a short run of 3 or 5 questions, one at a time, that helps you remember your experience and turn it into draft facts. You can start one from **Today** (the **Suggested session** block), from **Fact bank > Coverage**, from a job's gap check ("Answer a few questions about these gaps"), from an interview on an application ("Talk through this interview in a session"), from a rejected application ("Start a session about this role's requirements"), from a capture note ("Expand in a session") or from a quality hint such as "Add a result?" ("Answer in a session"). Sessions need an AI provider. Each question and answer counts toward your usage budget, like other AI work, and a budget limit stops a start before anything is queued. Only one session can be open at a time; **Continue your session** returns to it.

Each question comes with a short "Why this question" line. You can answer in your own words, or press **Skip**, **I don't remember** (it returns later from a different angle), **Ask me later** (next session, in a week, in a month or on a date) or **End session**. After an answer, the app turns it into a draft fact or story. Any detail that your answer does not state is shown as "This isn't in your answer", and the draft needs your check. When the session ends, **Confirm**, **Edit**, **Discard** or **Leave in Drafts to review** each draft. Nothing becomes a confirmed fact until you confirm it.

The app checks each question before you see it. A question that asks for a number, employer or skill your facts do not state, opens with a yes/no word or ends with a tag such as "right?" is replaced by a fixed open question. This check reads English only. Your answers, questions and drafts stay in your local database. Discarding a draft does not delete the answer you typed, and the app has no way to purge stored answers yet.

**Coverage** (**Fact bank > Coverage**) lists the requirements that your saved and active jobs ask for, grouped by similar wording, with the biggest gap first. Each row shows how many saved jobs ask for it and how strong your evidence is: **Strong** (two or more confirmed stories), **Thin** (facts, or one story) or **None**. **Start session** on a row starts a session about that requirement. **Strengthen** lists the facts behind a Thin row, each with a link to **Refine with AI**. Grouping uses English wording only, so the same requirement written in different words can appear as two rows.

**Settings > Sessions** sets how often a session is suggested (never, weekly, twice a week or after each application), the default length, the default for "Ask me later", and **Pause all suggestions**. The section is a collapsed disclosure: open it to see the controls. A suggestion shows on Today only when one is due and you have questions to answer; **Dismiss** hides it until the next due time.

## Interview prep

Each application has an **Interview prep** tab. It builds a set of likely questions from the job and the CV you sent, and it reuses questions you have practised or been asked before. **Web questions** search the web for questions other people report, and each one shows its source and date. You can practise an answer for feedback, build narrative cards and an adapted answer, and record a **debrief** after an interview from Today. On a company page, **Get a company brief** searches the web for a few sourced facts about the company.

What leaves this computer: the job summary, the requirements you have not covered, and the CV you sent go to the AI for questions. Practice answers and feedback go to the AI when you ask for them. The "how it went" part of a debrief is never sent. Web questions and company briefs send the job or company name, role title, website and industry, and the search provider runs its searches. Everything you write, and every saved question, debrief and brief, stays in your local database. A web result is kept only when the search provider's terms allow it; otherwise it is shown until the app closes. The built-in Claude search is treated as allowed to store its results, as you confirmed. Each AI and web request counts toward your usage budget.

Check everything the AI writes against your own experience. Practice feedback flags any claim that is not in your fact bank. Draft facts from a debrief or practice need your check before you use them. The checks are approximate and English-only, so they can miss a claim.

## Job inbox

**Add a job.** In the Job inbox, choose **+ Add job** and paste a link to the posting, or paste the job text (with the link to the original, if you like). A link is fetched once, when you press the button, with "Fetching the page…" shown while it works. If the page can't be fetched or has no readable job text, for example a page that needs JavaScript or a sign-in, or an address the app refuses, the form says so and opens the text box: paste the ad there and the link is kept as the link to the original. The app fetches only the one address you give it, never follows links on the page, and refuses addresses that point at your own computer or network. A pasted link is stored and never fetched. Job text over 50,000 characters is refused rather than cut. The same link again adds a source, not a second job, and a near-identical posting on another site is merged into the existing card ("also posted on N sites"); **Not a duplicate** in the job's Sources list splits it back and remembers the choice. The job is saved first, so you can triage it even when no AI provider is connected.

**Triage.** The queue shows new jobs, then seen ones, beside one focused card. A card is marked seen once it is shown. On a card you can:

- **Snooze** until tomorrow, next week, in a month or a date you pick. It returns as new on that date.
- **Remove**, with an optional one-tap reason (too junior, too senior, location, salary, industry, company, not interested or other; the last two can take a few words). Undo stays for about 10 seconds, and the **Removed** view is the lasting way back.
- **Apply** marks the job "To apply" and creates an application in Preparing (with undo while it is untouched). You always submit applications yourself.

The views **Snoozed**, **Removed** and **To apply** sit beside the queue. You can filter by fit, work mode, industry, seniority, company and "conflicts with your preferences", search the text, sort, and group by company, industry, fit, work mode or seniority. A group's count covers the whole filter even when only part of it is on this page. **Saved views** keep a name plus filters. When a summary contains a value the app doesn't know yet (an industry, say), a notice "N new values to map" opens a page where you can map it to an existing value, add it as a new value, or ignore it.

**Details.** Enter or the title opens the job's details: the summary, each requirement with your own current fact wording (linked to the fact) or "Missing", the saved text with its capture date, a link to the original, its sources and your notes. Fields such as industry, salary or seniority can be corrected; a field you corrected is marked "Corrected by you" and a later "Summarize again" never replaces it. A summary is made again only when you press Summarize again, and the fit is assessed again with "Assess fit again"; both go through the usage budget check.

**Keyboard shortcuts.** **J** next job, **K** previous, **A** mark to apply, **R** remove, **Enter** open the details, **Z** undo, **Esc** close and **?** show the list. They are ignored while you type. Single keys can clash with speech input or assistive technology, so Settings > Keyboard shortcuts has a switch, "Use single-key shortcuts"; with it off every action still has a button, and **?** still lists the keys. Every action announces what happened to screen readers.

**How you match.** After a summary, the app compares each requirement with your confirmed facts and shows the result as Strong, Possible or Stretch, with the requirements you meet and the ones you are missing. The band is worked out by the app, not by the AI: a requirement counts as met only if the AI cites facts that were actually sent in that request. Jobs that conflict with your preferences (a deal-breaker, or a stated salary below your minimum) are flagged "Conflicts with your preferences", never hidden. This is a guide, not a verdict: the app checks that references are real, not that every match is true, so read the linked facts yourself. Fit needs at least one usable confirmed fact; without one the card says "Add confirmed facts to see how you match" and no request is made.

**What is sent to the AI.** Adding a job makes up to two requests, a summary and then a fit assessment, and both count toward your usage budget (the warning says "Adding a job uses up to 2 requests"). The summary request contains the job text and the app's lists of industries, work modes and so on, nothing else. The text is treated as data, not instructions: it is delimited and anything in it that looks like a command is ignored. The fit request contains the job text and its requirements, your confirmed facts (their wording and ids; facts marked "Never use in applications" are left out, and "Generalize names" is applied as in Refine with AI), and your saved job preferences, all of them as set in Settings: target roles, locations, work modes, minimum salary (amount and currency), must-haves, deal-breakers and CV country. The AI's answer is checked by the app before anything is saved. The fetched address is never written to the logs, and job text, your notes and removal reasons stay out of logs, task records and event messages. They remain in your local database. The practice provider labels its fixed fictional answers "Practice data (fake provider)".

## Applications and Today

**Board** tracks your applications in status columns. Move a card with **Move to…** using the keyboard or mouse; on a wide screen you can also drag it. Closed applications have a collapsed section. **Table** offers a sortable, paginated view. Settings > Board lets you add statuses, assign each to Preparing, Active or Closed, and edit reminder rules. Built-in statuses cannot be deleted; a custom status can be deleted only while unused. Every status change stays in the application's history. The first move to an active status sets the applied date; it does not submit an application or freeze documents.

Open an application from a card or table row to see its drawer: status, timeline, notes, contacts, interactions and events. **Open workspace** opens the full application page. Moving to Interviewing offers an optional interview date; Rejected offers optional feedback. Skip or Escape keeps the status change. Notes and feedback can be edited. Events can be edited or cancelled. Contacts and interactions cannot yet be deleted, and interactions cannot yet be edited.

**Calendar files.** Add an interview, follow-up, deadline or offer deadline, with a date and IANA time zone such as `Europe/Paris`. Timed events also take a time and duration; deadlines can be all day. **Download calendar file** saves a `.ics` file for you to import into your calendar app. Check its time and zone there. This is a file export; there is no calendar sync. A time in a spring clock-change gap is refused; an ambiguous autumn time uses the first occurrence with a notice. The file preserves the stored instant, including an event that ends in the repeated hour.

**Today** shows upcoming interviews in the next seven days, due reminders, unseen jobs (with up to three Strong matches), fact drafts to review and document drafts awaiting approval, a suggested session (or, while a session is open, **Continue your session**) and optional setup steps still to finish, in that order. Empty blocks are hidden; when none needs attention it says so. Application counts appear in words.

**Notifications** contains due reminders and a link to tasks needing attention. Opening it marks only the reminders shown as seen. Done, Dismiss and Snooze are separate actions, shared with Today. Cards show "Follow-up due" in words. Reminders are computed when you read them: they work only inside the running app, with no background scheduler, email, push notification or work while the app is closed. A thank-you reminder does not draft a message. A completed event reminder does not become new when that same event is rescheduled; create a new event if you need a new reminder.

Contact names, emails and phones, interaction notes, application notes, feedback and event details stay in the local database and out of logs, task records, usage records and live-update messages. They are not sent to AI by these features. As with the Fact bank and job pages, another process on your computer can load local pages and read their contents; the launch token protects browser actions, not local-user access.

## Companies

**Companies** lists every company your jobs came from, sortable by name, open roles, jobs marked to apply and last activity, 50 per page. A company page shows its website (shown as a link only if it is an http or https address), industry, size, locations and notes, which you can edit, and every job at the company with its state, each linking to the job's page. Companies are created when a summary names one, matched by name ignoring case, punctuation and endings such as "Inc" or "Ltd". **Renaming a company changes how it is matched:** a job added later under the old name creates a new company instead of joining the renamed one. Merging two companies is not available yet.

Company pages also hold contacts and application history. Link a company contact to an application, or add one from its drawer; log interactions with an optional contact. A recent application at the same company produces an informational re-apply note, never a block. Renaming a company also affects its contacts and history; merging companies is still unavailable.

## Usage budgets

Settings > Usage budgets sets a monthly limit for each service: Claude subscription, Claude API key, Web search and, in the demo, the practice provider. A service has no limit until you set one. A budget has an allowance, a first and a second warning, a blocking point (at most the allowance), a reset day, and optional caps for the Fast and Thorough tiers. Days 29 to 31 mean the last day of shorter months. Limits count requests; token counts are shown but not used for limits yet.

Each warning appears once per cycle, in a dialog that asks you to continue. At the blocking point the action is disabled and says when the count resets. The usage panel above the forms shows each service's count, its reset date and any calls still running. Claude's own plan usage is shown for information only and never blocks an action. A connection test counts as one request and is never blocked.

Connection tests, CV import, Refine with AI, job summaries, fit assessments, session questions and answers, and practice calls count toward usage, including failed calls and AI correction attempts. Web search budgets take effect once web search is used. "Background tasks at once" sets how many tasks run together; a change applies the next time you start the app.

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
