"""Support for the prompt override tests (M9.3 part A; spec 3.3 and 3.6, ADR-055).

Builds a small fictional defaults folder in a temporary directory (so a test can bump the
default version, or change its inputs, the way a new release would), writes override files
byte by byte, and snapshots a folder tree so a test can show that nothing was written.
Everything is fictional; no network, no real data folder.
"""

import hashlib
import os
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any

TASK = "_test_task"
MARKER = "kestrel-marker-0042"  # planted in text that a message must never repeat
MARKER_NAME = "kestrel_marker_0042"
MAX_FILE_BYTES = 256 * 1024

DEFAULT_SYSTEM = "You read a fictional job posting for a fictional job seeker.\nAnswer in JSON."
DEFAULT_USER = (
    "Preferences:\n"
    "{{ preferences | tojson }}\n\n"
    '{{ job_text | untrusted("job_posting", "posting-1") }}'
)
EDIT_SYSTEM = "You summarize a fictional posting for a fictional reader.\nRespond with JSON only."
EDIT_USER = (
    "Preferences: {{ preferences | tojson }}\n\n"
    '{{ job_text | untrusted("job_posting", "posting-1") }}\n\n'
    "Answer in JSON."
)
SCHEMA = '{"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}'


def front_matter(
    *,
    version: int = 3,
    task: str = TASK,
    prompt_id: str = TASK,
    tier: str = "fast",
    inputs: Sequence[str] = ("job_text", "preferences"),
    untrusted: Sequence[str] = ("job_text",),
) -> str:
    return (
        "---\n"
        f"id: {prompt_id}\n"
        f"version: {version}\n"
        f"task: {task}\n"
        f"default_tier: {tier}\n"
        "output_schema: schema.json\n"
        f"inputs: [{', '.join(inputs)}]\n"
        f"untrusted_inputs: [{', '.join(untrusted)}]\n"
        "---\n"
    )


def prompt_file(system: str, user: str, **meta: Any) -> bytes:
    """A prompt.md in the loader's format; ``meta`` overrides front matter fields."""
    return (front_matter(**meta) + f"\n## system\n\n{system}\n\n## user\n\n{user}\n").encode()


def write_default(
    root: Path, *, system: str = DEFAULT_SYSTEM, user: str = DEFAULT_USER, **meta: Any
) -> Path:
    """(Re)write the defaults folder ``root/defaults`` with the task ``_test_task``."""
    folder = root / "defaults" / TASK
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "prompt.md").write_bytes(prompt_file(system, user, **meta))
    (folder / "schema.json").write_text(SCHEMA, encoding="utf-8")
    return root / "defaults"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_override_bytes(overrides: Path, data: bytes, *, task: str = TASK) -> Path:
    folder = overrides / task
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "prompt.md"
    path.write_bytes(data)
    return path


def snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Every entry under ``root`` (not following links): kind, mode, bytes or target, mtime."""
    found: dict[str, tuple[Any, ...]] = {}
    if not os.path.lexists(root):
        return found
    for path in sorted(root.rglob("*")):
        info = os.lstat(path)
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            found[relative] = ("link", os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            found[relative] = ("dir", stat.S_IMODE(info.st_mode), info.st_mtime_ns)
        else:
            found[relative] = (
                "file",
                stat.S_IMODE(info.st_mode),
                path.read_bytes(),
                info.st_mtime_ns,
            )
    return found


def files_under(root: Path) -> list[str]:
    """Relative names of every file (and link) under ``root``, folders left out."""
    if not os.path.lexists(root):
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if not stat.S_ISDIR(os.lstat(path).st_mode)
    )
