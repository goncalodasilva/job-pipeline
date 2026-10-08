"""Pretend the Claude Agent SDK is installed with or without its bundled CLI (M1.15).

The app looks for the CLI bundled with the SDK next to ``claude_agent_sdk.__file__``
(``_bundled/claude``, ``claude.exe`` on Windows; ADR-045, ADR-057). Tests point that file at a
folder under ``tmp_path`` so provider health is the same on every operating system. The
"binaries" are placeholders that are never run. Without a bundled CLI, ``PATH`` and
``USERPROFILE`` also point at empty folders, so the Windows lookup finds no native
``claude.exe`` either (ADR-058).
"""

from pathlib import Path

import claude_agent_sdk
import pytest

BUNDLED_NAMES = ("claude", "claude.exe")


def install_fake_sdk(monkeypatch: pytest.MonkeyPatch, root: Path, *, with_cli: bool) -> Path:
    """Point ``claude_agent_sdk.__file__`` into ``root``; return the ``_bundled`` folder."""
    package = root / "claude_agent_sdk"
    bundled = package / "_bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    init = package / "__init__.py"
    init.write_text("# fictional SDK package, never imported\n", encoding="utf-8")
    if with_cli:
        for name in BUNDLED_NAMES:
            (bundled / name).write_bytes(b"#!/bin/sh\nexit 1\n")  # never run
    else:
        empty_path = root / "empty-path"
        empty_profile = root / "empty-profile"
        empty_path.mkdir(exist_ok=True)
        empty_profile.mkdir(exist_ok=True)
        monkeypatch.setenv("PATH", str(empty_path))
        monkeypatch.setenv("USERPROFILE", str(empty_profile))
    monkeypatch.setattr(claude_agent_sdk, "__file__", str(init))
    return bundled
