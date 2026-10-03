# Job Search Pipeline

A local, open-source web app for running a job search: collect postings, see honestly how well you fit, position yourself for each role with a growing bank of your own facts, draft tailored CVs and cover letters, track every application on a board, and prepare for interviews. You always submit applications yourself.

**Status:** design complete; implementation starting at milestone M0. See `docs/PROGRESS.md`.

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

## Documentation

- `docs/TECHNICAL_SPEC.md`: architecture, rules, testing strategy, roadmap, and how the app is built with Claude Code
- `docs/PROGRESS.md`: what is done and what is next
- `docs/DECISIONS.md`: decisions and their reasons
- `docs/references/`: product spec and detailed design

## Developing with Claude Code

This repository includes a complete Claude Code setup: `CLAUDE.md`, subagents in `.claude/agents/`, skills in `.claude/skills/`, rules in `.claude/rules/`, and hooks in `.claude/hooks/`. Start with `/plan-milestone M0`, then `/next-task`. Details in `docs/TECHNICAL_SPEC.md` section 6.

## License

MIT. See `LICENSE`.
