#!/usr/bin/env python3
"""Translate shared hook results to Claude Code or Codex's native wire format."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Works both as `python scripts/agent_hook.py` and when imported by project tests.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.agent_support import evaluate, post_edit, project_root, string


def main(engine: str | None = None, kind: str | None = None) -> int:
    if engine is None or kind is None:
        if len(sys.argv) != 3:
            print("Usage: agent_hook.py <claude|codex> <session|pre|post>", file=sys.stderr)
            return 2
        engine, kind = sys.argv[1:]
    if engine not in ("claude", "codex") or kind not in ("session", "pre", "post"):
        return 2
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise ValueError("expected an object")
    except (ValueError, OSError):
        if kind == "pre":
            print("Project guard blocked malformed hook input.", file=sys.stderr)
            return 2
        data = {}
    if kind == "session":
        from scripts.session_context import context

        root = project_root(Path(string(data.get("cwd")) or str(Path.cwd())))
        output = context(root, engine)
        if engine == "codex":
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": output,
                        }
                    }
                )
            )
        else:
            print(output)
        return 0
    if kind == "post":
        return post_edit(data)
    decision = evaluate(data)
    if decision.action == "allow":
        return 0
    if decision.action == "ask" and engine == "claude":
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": decision.reason,
                    }
                }
            )
        )
        return 0
    reason = decision.reason
    if decision.action == "ask":
        reason += (
            " Codex hook maintenance: see docs/development/agents.md; do not bypass hook trust."
        )
    print(f"Blocked by project guard: {reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
