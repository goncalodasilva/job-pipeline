"""A fake Claude Code CLI for offline subprocess tests (M1.14, spec 4.2; ADR-045, ADR-057).

Run as a program (never imported by the app): tests write a small executable wrapper to a
temporary folder (``#!<python>`` shebang, mode 755) that runs this file, and pass the
wrapper as the provider's CLI path. The SDK's real ``SubprocessCLITransport`` then starts
it, so the command line, environment, working folder and stream-json protocol are the
ones a real CLI would get. Nothing here touches the network. Standard library only.

Behaviour:

- ``-v`` / ``--version``: prints ``2.1.286 (Claude Code)`` and exits.
- Otherwise reads stream-json lines on stdin: every ``control_request`` (``initialize``)
  gets a success ``control_response``; the first ``user`` frame gets a fictional system
  init frame, an assistant frame and a success result whose ``structured_output`` is
  ``{"ok": true}``. With ``JOBPIPELINE_FAKE_CLI_SCENARIO=auth_failed`` it answers with the
  CLI's authentication failure pair instead (assistant ``authentication_failed`` and a
  401 error result) and exits 1 once stdin closes. With ``silent`` it answers the control
  protocol but sends nothing after the user frame, and exits once stdin closes; with
  ``silent_ignore_eof`` it also ignores stdin closing and waits until it is signalled
  (only a terminate or kill stops it). Both model a CLI that went quiet (M2.13).
- Writes its own process id to the path in ``JOBPIPELINE_FAKE_CLI_PID`` (if set) when the
  user frame arrives, so tests can check the process is gone afterwards.
- When the user frame arrives, writes a JSON report to the path in
  ``JOBPIPELINE_FAKE_CLI_REPORT`` (if set): argv, cwd, the cwd listing, selected
  environment values (``None`` when absent), the listing of ``CLAUDE_CONFIG_DIR``, the
  text of the ``--system-prompt-file`` file and the prompt it received.

All content is fictional.
"""

import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

VERSION_TEXT = "2.1.286 (Claude Code)"
MODEL = "claude-haiku-fictional-cli"
REPORT_VARIABLE = "JOBPIPELINE_FAKE_CLI_REPORT"
SCENARIO_VARIABLE = "JOBPIPELINE_FAKE_CLI_SCENARIO"
PID_VARIABLE = "JOBPIPELINE_FAKE_CLI_PID"
SILENT_SCENARIOS = frozenset({"silent", "silent_ignore_eof"})
REPORTED_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)
SESSION = "session-fictional-cli-0001"


def _emit(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def _flag_value(argv: list[str], flag: str) -> str | None:
    for index, value in enumerate(argv):
        if value == flag and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith(flag + "="):
            return value[len(flag) + 1 :]
    return None


def _listing(folder: str | None) -> list[str] | None:
    if not folder or not os.path.isdir(folder):
        return None
    return sorted(os.listdir(folder))


def _write_report(argv: list[str], prompt: Any) -> None:
    target = os.environ.get(REPORT_VARIABLE)
    if not target:
        return
    system_file = _flag_value(argv, "--system-prompt-file")
    system_text = None
    if system_file is not None and os.path.isfile(system_file):
        system_text = Path(system_file).read_text(encoding="utf-8")
    report = {
        "argv": argv,
        "cwd": os.getcwd(),
        "cwd_listing": sorted(os.listdir(os.getcwd())),
        "env": {name: os.environ.get(name) for name in REPORTED_VARIABLES},
        "config_dir_listing": _listing(os.environ.get("CLAUDE_CONFIG_DIR")),
        "system_prompt_file": system_file,
        "system_prompt_text": system_text,
        "prompt": prompt,
    }
    Path(target).write_text(json.dumps(report), encoding="utf-8")


def _answer_success() -> None:
    _emit(
        {
            "type": "assistant",
            "session_id": SESSION,
            "uuid": "uuid-fictional-cli-0002",
            "parent_tool_use_id": None,
            "message": {
                "id": "msg-fictional-cli-0001",
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": [{"type": "text", "text": "Fictional answer."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 9, "output_tokens": 2},
            },
        }
    )
    _emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 10,
            "duration_api_ms": 8,
            "num_turns": 1,
            "session_id": SESSION,
            "uuid": "uuid-fictional-cli-0003",
            "stop_reason": "end_turn",
            "result": "Fictional answer.",
            "structured_output": {"ok": True},
            "usage": {"input_tokens": 9, "output_tokens": 2},
            "terminal_reason": "completed",
        }
    )


def _answer_auth_failed() -> None:
    _emit(
        {
            "type": "assistant",
            "session_id": SESSION,
            "uuid": "uuid-fictional-cli-0012",
            "parent_tool_use_id": None,
            "error": "authentication_failed",
            "message": {
                "id": "msg-fictional-cli-0002",
                "type": "message",
                "role": "assistant",
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "Fictional: invalid credentials."}],
                "stop_reason": "stop_sequence",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }
    )
    _emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": 401,
            "duration_ms": 10,
            "duration_api_ms": 0,
            "num_turns": 1,
            "session_id": SESSION,
            "uuid": "uuid-fictional-cli-0013",
            "result": "Fictional: invalid credentials.",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "terminal_reason": "api_error",
        }
    )


def _write_pid() -> None:
    target = os.environ.get(PID_VARIABLE)
    if target:
        Path(target).write_text(str(os.getpid()), encoding="utf-8")


def _wait_for_signal() -> None:
    """Block until a signal ends the process (SIGTERM or SIGKILL by default)."""
    while True:
        signal.pause()


def main(argv: list[str]) -> int:
    if "-v" in argv or "--version" in argv:
        print(VERSION_TEXT)
        return 0
    scenario = os.environ.get(SCENARIO_VARIABLE, "success")
    answered = False
    for line in sys.stdin:
        if not line.strip():
            continue
        frame = json.loads(line)
        kind = frame.get("type")
        if kind == "control_request":
            _emit(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": frame.get("request_id"),
                        "response": {},
                    },
                }
            )
        elif kind == "user" and not answered:
            answered = True
            _write_report(argv, frame.get("message", {}).get("content"))
            _write_pid()
            if scenario in SILENT_SCENARIOS:
                continue  # nothing is ever sent after the prompt
            _emit(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": SESSION,
                    "uuid": "uuid-fictional-cli-0001",
                    "cwd": os.getcwd(),
                    "model": MODEL,
                    "tools": [],
                    "mcp_servers": [],
                    "permissionMode": "dontAsk",
                    "apiKeySource": "none",
                }
            )
            if scenario == "auth_failed":
                _answer_auth_failed()
            else:
                _answer_success()
    if scenario == "silent_ignore_eof":
        _wait_for_signal()
    return 1 if scenario == "auth_failed" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
