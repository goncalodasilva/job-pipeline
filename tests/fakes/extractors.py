"""Text extractors for the deadline and child-process tests (M3.1, spec 2.9 rule 5).

``extract_text`` parses in a child process. These stand in for a parser that hangs, crashes or
fails, so no test needs a huge or hostile file. They are plain module-level classes holding
only paths, so they can be handed to a child process (pickled, or imported by name as
``tests.fakes.extractors``).
"""

import importlib.util
import json
import os
import time
from pathlib import Path

HANG_SECONDS = 60.0
MARKER_VARIABLE = "JOBPIPELINE_TEST_FICTIONAL_MARKER"


def _record_pid(pid_file: Path) -> None:
    pid_file.write_text(str(os.getpid()), encoding="utf-8")


class RecordingExtractor:
    """Returns the file's text and records which process read it."""

    kind = "text"

    def __init__(self, pid_file: Path) -> None:
        self.pid_file = pid_file

    def extract(self, path: Path) -> str:
        _record_pid(self.pid_file)
        return path.read_text(encoding="utf-8")


class SlowExtractor:
    """Records its process id, then hangs far longer than any test deadline."""

    kind = "pdf"

    def __init__(self, pid_file: Path) -> None:
        self.pid_file = pid_file

    def extract(self, path: Path) -> str:
        _record_pid(self.pid_file)
        time.sleep(HANG_SECONDS)
        return "never returned"


class CrashingExtractor:
    """Ends its process abruptly, as a parser crash in native code would."""

    kind = "pdf"

    def __init__(self, pid_file: Path) -> None:
        self.pid_file = pid_file

    def extract(self, path: Path) -> str:
        _record_pid(self.pid_file)
        os._exit(7)


class BrokenExtractor:
    """Raises an error no parser is supposed to raise."""

    kind = "pdf"

    def extract(self, path: Path) -> str:
        raise RuntimeError("parser bug with no user-facing meaning")


def _cpu_limit() -> int | None:
    """The soft CPU-time limit of this process, or None where there is no such limit."""
    if importlib.util.find_spec("resource") is None:
        return None
    resource = importlib.import_module("resource")
    soft: int = resource.getrlimit(resource.RLIMIT_CPU)[0]
    return None if soft == resource.RLIM_INFINITY else soft


def _stderr_is_null_device() -> bool:
    with open(os.devnull, "rb") as null:
        return os.path.sameopenfile(2, null.fileno())


class ChildStateExtractor:
    """Answers with what the parsing process can see and how it is limited, as JSON."""

    kind = "text"

    def extract(self, path: Path) -> str:
        return json.dumps(
            {
                "marker_visible": MARKER_VARIABLE in os.environ,
                "variables": len(os.environ),
                "cpu_limit": _cpu_limit(),
                "stderr_is_null_device": _stderr_is_null_device(),
            }
        )


class OversizedExtractor:
    """Returns far more text than any document holds."""

    kind = "text"

    def __init__(self, characters: int) -> None:
        self.characters = characters

    def extract(self, path: Path) -> str:
        return "x" * self.characters
