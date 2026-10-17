@AGENTS.md

## Claude Code workflow

Use `/plan-milestone M<n>` → `/run-milestone` (all eligible tasks, ADR-065) or `/next-task` (one task) → `/milestone-acceptance M<n>`.

Keep the existing specialists: `planner`, `test-author`, `implementer`, `verifier`, `debugger`, `code-reviewer`, `security-reviewer`, `ux-reviewer`, `prompt-engineer`, `docs-keeper`. The workflow skills specify delegation and checkpoints; spec 6.3 lists review triggers. Native agent definitions remain in `.claude/agents/`.

The procedure skills in `.claude/skills/` load their shared counterparts in `.agents/skills/`; read those shared files and resolve their references from the shared directory. Engineering rules in `.claude/rules/` also remain authoritative for both agents.

Hooks in `.claude/hooks/` are compatibility entry points for `scripts/agent_hook.py`. Use `bash scripts/verify.sh quick|full`; the old verification and task-selector paths remain compatible. Review and update recurring lessons in `docs/development/review-notes/`, not duplicate native memory notes.
