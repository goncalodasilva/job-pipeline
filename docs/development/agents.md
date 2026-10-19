# Developing with Claude Code and Codex

Both tools use `AGENTS.md`, the same engineering rules, six shared procedures, verification scripts and repository handoff records (ADR-096). Application providers and user authentication are separate from the coding-tool setup.

## Setup

1. In each clone/worktree run `uv sync`, `uv run pre-commit install`, and, when needed, `uv run playwright install chromium`. Hooks need `python3` on PATH. On Windows use Git Bash and a Python installation exposing `python3`.
2. Claude Code reads `CLAUDE.md`, which imports `AGENTS.md`. Accept the workspace trust prompt. Its existing agents, rules and slash commands remain available.
3. Open Codex in the repository and trust the project configuration. Review project hooks using `/hooks` in Codex CLI, or the client's hook-review UI. New or changed non-managed hook definitions are skipped until trusted. Do not bypass hook trust. Also review changed hook scripts: an unchanged command's definition hash does not attest to its script contents.
4. Confirm instruction discovery and skill availability in a fresh session. Codex discovers `.agents/skills/`; Claude discovers `.claude/skills/`. Run the status command and quick verification below before handing off implementation.

This setup targets local Codex clients with project lifecycle hooks and custom TOML agents. The initial investigation used bundled Codex CLI `0.162.0-alpha.17.2` and Claude Code `2.1.286`. Client support and managed policies can differ. A feature reported enabled does not prove project hooks are trusted or firing. Codex CLI `codex debug prompt-input` inspects effective context without a model request; inspect its output locally rather than publishing private/global instructions.

```bash
python3 scripts/next_task.py --open
bash scripts/verify.sh quick
```

Native Codex config requests workspace-write, on-request approval and no sandbox network access. Higher-priority managed settings still apply. Install dependencies/browsers once. If cache access or a smoke/e2e loopback server is blocked, use native approval for the specific command and rerun; do not disable the sandbox globally. Network-free tests still use the project's fakes. Live-provider checks remain user-run.

## Entry points

| Purpose | Claude Code | Codex request |
| --- | --- | --- |
| Plan | `/plan-milestone M3` | `$plan-milestone M3` |
| One task | `/next-task M3.8` | `$next-task M3.8` |
| Several tasks | `/run-milestone M3 --max 2` | `$run-milestone M3 --max 2` |
| Verification | `/verify quick` | `$verify quick` |
| Acceptance | `/milestone-acceptance M3` | `$milestone-acceptance M3` |

In Codex CLI/IDE use `$` or the skill selector. In clients with a different selector, attach the matching skill and include the task/milestone and options in the message. Natural-language requests can explicitly ask for the corresponding workflow; read its skill instructions before following it. Planning, task execution, milestone execution and acceptance have implicit invocation disabled. Verification and the six procedures can be selected when relevant.

Claude keeps its ten specialists. Codex normally handles understanding, failing tests, implementation, verification and documentation in the main session, then delegates an independent code review and any triggered security/UX/prompt review. The four `.codex/agents/*.toml` profiles inherit the selected model and permissions. If a client cannot select a custom profile, the main session passes that profile's instructions to a generic subagent. A role's instruction to avoid edits is behavioral; it does not create a separate read-only sandbox. If subagents are unavailable, independent review remains outstanding.

`PROGRESS.md` and ADR-065 define the same checkpoints, task parking, completion evidence and user acceptance in both tools. Neither tool may accept a milestone on the user's behalf.

## Shared files and compatibility

- `AGENTS.md`: shared project contract and explicit path-to-rule routing for Codex. Claude also loads the `.claude/rules/` files natively.
- `.agents/skills/{add-ai-task,add-provider,background-task,db-migration,htmx-ui,onboarding-step}`: maintained procedure bodies and references. Claude's same-name skills are small routing wrappers. Resolve references from the shared skill directory.
- `.agents/skills/{plan-milestone,next-task,run-milestone,verify,milestone-acceptance}`: native Codex workflows; Claude's orchestration skills remain in `.claude/skills/`.
- `scripts/verify.sh`, `scripts/next_task.py`, `scripts/session_context.py`: shared verification, task selection and status summary. Old `.claude/skills/.../scripts/` paths forward to these helpers.
- `scripts/agent_support.py`: shared policy/path normalization and Python edit feedback. `scripts/agent_hook.py` translates native hook responses. Old `.claude/hooks/` files are compatibility entry points; Codex hooks call the shared adapter directly.
- `docs/development/review-notes/`: shared recurring review lessons. Claude's native memory files route here; Codex reviewers return proposed lessons to the parent to record.

Keep full procedures and review lessons in those maintained locations. Do not grow separate copies in wrappers or native memory indexes. The legacy prompt-reference path remains a pointer; read the full prompt format from the shared skill.

## Hooks and permissions

Both clients configure SessionStart, PreToolUse and PostToolUse. Session context reloads current progress after startup/resume/clear/compaction; Codex also covers forks. The pre-tool adapter normalizes Claude file tools and shell commands and Codex multi-file patches, including rename sources/destinations and deletions. It checks secret paths (also resolved symlinks), real data/config paths and common destructive/secret-printing commands. Malformed policy input and unrecognized file/patch payloads block with a reason; informational/post-edit failures remain advisory.

The post-edit adapter formats/lints existing changed Python files using offline, no-sync uv; deleted files are ignored, renamed destinations are included, and remaining ruff failures are reported. Completion always requires explicit verification even if edit feedback was clean or unavailable.

These hooks catch common mistakes. They do not parse arbitrary programs, cover every connector/read path, or provide an OS security boundary. Native sandbox/approval controls and pre-commit/CI remain additional layers. Codex `.rules` files request native approval for common push/history/dependency/deletion commands; Claude retains its native allow/ask/deny configuration. Do not copy one tool's permission syntax into the other.

### Approved hook maintenance

Changes to either tool's settings/hooks/rules or the shared hook policy require explicit user authorization. Claude uses its native PreToolUse `ask` response. Current Codex cannot implement `permissionDecision: ask`; its adapter blocks and points here instead of returning unsupported output that would let the call continue.

After the user authorizes the specific maintenance, the user can disable only this project's PreToolUse hook in `/hooks` (or the hook-review UI), let the authorized changes be made, then re-enable it and review/trust changed definitions. Review changed shared scripts as well. Re-run adapter regression tests and synthetic deny probes before continuing normal work. The agent must not disable hooks, alter trust state, set a bypass environment variable or bypass the sandbox on its own. Do not use permanent maintenance switches.

## Switching tools and parallel work

Prefer switching at a committed task boundary. For unfinished work, append a session-log handoff naming the task, completed phase, changed files, exact checks/results, outstanding review, blocker/checkpoint and next action. Mark results uncommitted when applicable; do not infer a passing check from old Evidence. The receiving agent inspects status and the diff and resumes at the actual phase.

One writer owns a checkout. Parallel implementation requires separate worktrees, disjoint files and one coordinator; the coordinator alone updates shared progress and integrates each task after verification/review. Do not parallelize migrations, the same module or shared templates. Preserve milestone branch conventions for integrated work. Do not automatically stash/commit unrelated changes, push or merge.

## Verification of this setup

The initial local validation discovered all 11 project skills through the native app server with no project skill errors. Strict native config loading accepted the project layer, workspace-write/on-request settings and inherited-model agent configuration. Native execution-policy checks requested approval for pushes and dependency additions. The three project hook definitions were registered/enabled and still untrusted; no trust state was changed. `debug prompt-input` confirmed the root contract; use `skills/list`/the native selector to inspect the skill catalog, which that messages-only diagnostic does not include.

Final `VERIFY quick: PASS`: 4,915 fast tests and 11 smoke tests, format/lint, strict types and all 13 import contracts passed. The 78 tooling cases are included in that run; no live-provider calls or application/UI changes were made.

Independent code and security reviews approved the adapters after regressions for operation working directories, equality-form paths and shell punctuation were fixed. A read-only workflow simulation covered parked/dependent tasks, stale uncommitted evidence and first-task checkpoints. These checks do not establish native trusted event dispatch.

Run `uv run pytest tests/unit/test_agent_tooling.py -q` for policy, native command adapters, path/rename handling, post-edit feedback and helper compatibility. Run the shared verification recipe before committing. Validate skill frontmatter with the installed skill-creator validator when changing skills.

After trusting hooks in a fresh native session, confirm SessionStart shows the current milestone. Use synthetic temporary files/payloads to confirm safe Python edit feedback and a secret-path denial. Do not probe real secret or personal-data contents. Record native activation evidence separately from subprocess/schema checks: configuration parsing and direct hook invocation do not prove event dispatch by the client.
