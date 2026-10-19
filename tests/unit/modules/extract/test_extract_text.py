"""``extract_text``: text out of a stored upload, parsed in a child process with a deadline
(M3.1; AC-M3.7; spec 2.9 rule 5; ADR-089 items 2 and 11).

``extract_text(path, kind, timeout, *, extractor=None)`` returns the text or raises
``UnreadableDocument`` (damaged; ``EncryptedDocument`` for a password-protected PDF),
``NoTextFound`` (nothing to read, as in a scan) or ``ParseTimedOut``. ``extractor`` replaces
the parser chosen by ``kind``; the tests use it to stand in for a parser that hangs or crashes.
Every file is fictional (``tests/fixtures/cv/``). The new names are imported inside the tests.
"""

import importlib
import importlib.util
import json
import math
import os
import time
from pathlib import Path
from typing import Any

import pytest

from tests.cv_fixtures import assert_markers_in_order, fixture_path, one_space
from tests.fakes.extractors import (
    HANG_SECONDS,
    MARKER_VARIABLE,
    BrokenExtractor,
    ChildStateExtractor,
    CrashingExtractor,
    OversizedExtractor,
    RecordingExtractor,
    SlowExtractor,
)

# Generous: a healthy parse of these small files takes far less, even on a busy machine.
PARSE_TIMEOUT = 30.0
# Short, but long enough for a child process to start and record its id.
DEADLINE = 2.0


def _extract_text(path: Path, kind: str, timeout: float, **options: Any) -> str:
    module = importlib.import_module("app.modules.extract")
    text: str = module.extract_text(path, kind, timeout=timeout, **options)
    return text


def _error(name: str) -> type[Exception]:
    errors = importlib.import_module("app.domain.errors")
    assert hasattr(errors, name), f"app.domain.errors.{name} doesn't exist yet"
    found: type[Exception] = getattr(errors, name)
    return found


def _is_running(pid: int) -> bool:
    """True while the process exists, a zombie that nobody waited for included."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _recorded_pid(pid_file: Path) -> int:
    assert pid_file.is_file(), "the parser never started in the child process"
    return int(pid_file.read_text(encoding="utf-8"))


PDF_FIXTURES = {
    "single": "single_column.pdf",
    "two_column": "two_column.pdf",
    "table": "table.pdf",
}


@pytest.mark.parametrize("layout", list(PDF_FIXTURES))
def test_pdf_text_matches_markers(layout: str) -> None:
    name = PDF_FIXTURES[layout]

    text = _extract_text(fixture_path(name), "pdf", PARSE_TIMEOUT)

    assert isinstance(text, str)
    assert_markers_in_order(text, name)


def test_pdf_text_covers_every_page() -> None:
    text = one_space(_extract_text(fixture_path("single_column.pdf"), "pdf", PARSE_TIMEOUT))

    # The first line of page 1 and the last line of page 2.
    assert "Alex Example" in text
    assert "SQL, Excel, Python, Portuguese, English" in text


def test_docx_text_includes_table_cells() -> None:
    text = _extract_text(fixture_path("with_tables.docx"), "docx", PARSE_TIMEOUT)

    assert_markers_in_order(text, "with_tables.docx")
    flat = one_space(text)
    # These appear only inside table cells.
    assert "Invoice time down 35% in 4 warehouses" in flat
    assert "60 delivery routes planned each week" in flat
    # The cells of one row stay separate words.
    assert "Example Widgets LtdOperations Analyst" not in flat


def test_plain_text_is_returned_as_written() -> None:
    text = _extract_text(fixture_path("cv.txt"), "text", PARSE_TIMEOUT)

    assert_markers_in_order(text, "cv.txt")
    assert "Évora" in text and "Português" in text


def test_plain_text_drops_a_byte_order_mark(tmp_path: Path) -> None:
    stored = tmp_path / "upload-0001"
    stored.write_bytes(b"\xef\xbb\xbf" + b"Alex Example\nOperations Analyst\n")

    text = _extract_text(stored, "text", PARSE_TIMEOUT)

    assert text.startswith("Alex Example")


def test_kind_decides_the_parser_not_the_file_name(tmp_path: Path) -> None:
    # Stored uploads have generated names without a meaningful extension (spec 2.9 rule 5).
    stored = tmp_path / "upload-0002.txt"
    stored.write_bytes(fixture_path("single_column.pdf").read_bytes())

    text = _extract_text(stored, "pdf", PARSE_TIMEOUT)

    assert_markers_in_order(text, "single_column.pdf")


def test_truncated_pdf_unreadable() -> None:
    with pytest.raises(_error("UnreadableDocument")) as caught:
        _extract_text(fixture_path("truncated.pdf"), "pdf", PARSE_TIMEOUT)

    assert not isinstance(caught.value, _error("EncryptedDocument"))


def test_encrypted_pdf_refused() -> None:
    unreadable = _error("UnreadableDocument")
    encrypted = _error("EncryptedDocument")
    assert issubclass(encrypted, unreadable)

    with pytest.raises(encrypted) as caught:
        _extract_text(fixture_path("encrypted.pdf"), "pdf", PARSE_TIMEOUT)

    # Nothing of the protected content leaks through the error.
    assert "Example Widgets" not in str(caught.value)


def test_image_only_pdf_no_text_found() -> None:
    with pytest.raises(_error("NoTextFound")) as caught:
        _extract_text(fixture_path("image_only.pdf"), "pdf", PARSE_TIMEOUT)

    assert "paste the text" in caught.value.user_message.lower()  # type: ignore[attr-defined]


def test_empty_docx_no_text_found() -> None:
    with pytest.raises(_error("NoTextFound")):
        _extract_text(fixture_path("empty_body.docx"), "docx", PARSE_TIMEOUT)


def test_docx_oversized_entry_refused() -> None:
    # The file is a valid Word file whose text a careless reader would return, so only a check
    # of the entry sizes makes this raise. It is refused, not left to run into the deadline.
    started = time.monotonic()

    with pytest.raises(_error("UnreadableDocument")):
        _extract_text(fixture_path("oversized_entry.docx"), "docx", PARSE_TIMEOUT)

    assert time.monotonic() - started < PARSE_TIMEOUT / 2


def test_zip_that_is_not_word_is_unreadable_as_docx() -> None:
    with pytest.raises(_error("UnreadableDocument")):
        _extract_text(fixture_path("not_word.zip"), "docx", PARSE_TIMEOUT)


def test_missing_file_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(_error("UnreadableDocument")) as caught:
        _extract_text(tmp_path / "gone", "pdf", PARSE_TIMEOUT)

    # The message shown to the user never names a path.
    assert str(tmp_path) not in caught.value.user_message  # type: ignore[attr-defined]


def test_unknown_kind_is_refused(tmp_path: Path) -> None:
    stored = tmp_path / "upload-0003"
    stored.write_bytes(b"Alex Example\n")

    with pytest.raises((_error("UnsupportedDocumentType"), ValueError)):
        _extract_text(stored, "png", PARSE_TIMEOUT)


def test_parse_runs_in_a_child_process(tmp_path: Path) -> None:
    stored = tmp_path / "upload-0004"
    stored.write_text("Alex Example\nOperations Analyst\n", encoding="utf-8")
    pid_file = tmp_path / "child.pid"

    text = _extract_text(stored, "text", PARSE_TIMEOUT, extractor=RecordingExtractor(pid_file))

    assert one_space(text) == "Alex Example Operations Analyst"
    pid = _recorded_pid(pid_file)
    assert pid != os.getpid(), "the document was parsed inside the app's own process"
    assert not _is_running(pid), "the child process was left behind after a successful parse"


def test_parse_killed_at_deadline(tmp_path: Path) -> None:
    stored = tmp_path / "upload-0005"
    stored.write_bytes(fixture_path("single_column.pdf").read_bytes())
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()

    with pytest.raises(_error("ParseTimedOut")):
        _extract_text(stored, "pdf", DEADLINE, extractor=SlowExtractor(pid_file))

    elapsed = time.monotonic() - started
    assert elapsed >= DEADLINE * 0.9, "gave up before the deadline"
    assert elapsed < HANG_SECONDS / 4, "waited for the parser instead of stopping at the deadline"
    pid = _recorded_pid(pid_file)
    assert pid != os.getpid()
    # Killed and waited for: not running, and not left as a zombie either.
    assert not _is_running(pid), "the parser's process is still there after the deadline"


def test_parser_crash_is_unreadable_not_a_hang(tmp_path: Path) -> None:
    stored = tmp_path / "upload-0006"
    stored.write_bytes(fixture_path("single_column.pdf").read_bytes())
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()

    with pytest.raises(_error("UnreadableDocument")):
        _extract_text(stored, "pdf", PARSE_TIMEOUT, extractor=CrashingExtractor(pid_file))

    assert time.monotonic() - started < PARSE_TIMEOUT / 2, "a dead child was waited for"
    assert not _is_running(_recorded_pid(pid_file))


def test_unexpected_parser_error_is_unreadable_without_its_text(tmp_path: Path) -> None:
    stored = tmp_path / "upload-0007"
    stored.write_bytes(fixture_path("single_column.pdf").read_bytes())

    with pytest.raises(_error("UnreadableDocument")) as caught:
        _extract_text(stored, "pdf", PARSE_TIMEOUT, extractor=BrokenExtractor())

    # Error text from a parser can quote the document; only the type may be kept (spec 2.10).
    assert "parser bug" not in str(caught.value)
    assert "parser bug" not in caught.value.user_message  # type: ignore[attr-defined]


# --- the child process is boxed in -----------------------------------------------------------


def _runner() -> Any:
    return importlib.import_module("app.modules.extract.runner")


def _child_state(tmp_path: Path, timeout: float) -> dict[str, Any]:
    stored = tmp_path / "upload-0008"
    stored.write_text("Alex Example\n", encoding="utf-8")
    report = _extract_text(stored, "text", timeout, extractor=ChildStateExtractor())
    state: dict[str, Any] = json.loads(report)
    return state


def test_child_sees_no_inherited_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MARKER_VARIABLE, "fictional-value")

    state = _child_state(tmp_path, PARSE_TIMEOUT)

    assert state["marker_visible"] is False, "the parser can read the app's environment"
    assert state["variables"] == 0


@pytest.mark.parametrize(
    ("timeout", "expected"),
    [(30.0, 35), (2.0, 7), (0.5, 6), (30.2, 36)],
)
def test_cpu_limit_is_the_timeout_plus_five_seconds_rounded_up(
    timeout: float, expected: int
) -> None:
    assert _runner()._cpu_limit_seconds(timeout) == expected


def test_child_cpu_time_is_limited(tmp_path: Path) -> None:
    timeout = 41.5

    state = _child_state(tmp_path, timeout)

    if importlib.util.find_spec("resource") is None:
        assert state["cpu_limit"] is None  # no such limit on this platform
    else:
        assert state["cpu_limit"] == math.ceil(timeout) + 5


def test_child_stderr_goes_nowhere(tmp_path: Path) -> None:
    state = _child_state(tmp_path, PARSE_TIMEOUT)

    assert state["stderr_is_null_device"] is True


# --- the size of the answer ------------------------------------------------------------------


def test_nine_megabyte_text_file_is_extracted(tmp_path: Path) -> None:
    line = "Alex Example, Operations Analyst at Example Widgets Ltd\n"
    written = line * (9 * 1024 * 1024 // len(line))
    stored = tmp_path / "upload-0009"
    stored.write_text(written, encoding="utf-8")

    text = _extract_text(stored, "text", PARSE_TIMEOUT)

    assert text == written


def test_answer_over_the_cap_is_unreadable_and_reported_by_the_child(tmp_path: Path) -> None:
    stored = tmp_path / "upload-0010"
    stored.write_text("Alex Example\n", encoding="utf-8")
    cap: int = _runner()._MAX_ANSWER_BYTES
    assert cap >= 16 * 1024 * 1024, "a valid upload's text may not fit in the answer"

    with pytest.raises(_error("UnreadableDocument")) as caught:
        _extract_text(stored, "text", PARSE_TIMEOUT, extractor=OversizedExtractor(cap + 1))

    # The child said so itself; the parent didn't have to give up on an over-long answer.
    assert str(caught.value) == "the parser's answer passed the cap"


# --- a child that can't be started -----------------------------------------------------------


def test_extractor_that_cannot_be_sent_to_the_child_is_unreadable(tmp_path: Path) -> None:
    class LocalExtractor:  # defined inside a function, so it can't be pickled
        kind = "text"

        def extract(self, path: Path) -> str:
            return "never called"

    stored = tmp_path / "upload-0011"
    stored.write_text("Alex Example\n", encoding="utf-8")

    with pytest.raises(_error("UnreadableDocument")) as caught:
        _extract_text(stored, "text", PARSE_TIMEOUT, extractor=LocalExtractor())

    assert str(caught.value) in {"AttributeError", "PicklingError", "TypeError"}


def test_child_that_fails_to_start_closes_both_pipe_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    ends: list[Any] = []
    make_pipe = context.Pipe

    def recording_pipe(duplex: bool = True) -> Any:
        pair = make_pipe(duplex)
        ends.extend(pair)
        return pair

    def failing_start(process: Any) -> None:
        raise OSError("no more processes")

    monkeypatch.setattr(context, "Pipe", recording_pipe)
    monkeypatch.setattr(context.Process, "start", failing_start)
    stored = tmp_path / "upload-0012"
    stored.write_text("Alex Example\n", encoding="utf-8")

    with pytest.raises(_error("UnreadableDocument")) as caught:
        _extract_text(stored, "text", PARSE_TIMEOUT)

    assert str(caught.value) == "OSError"
    assert len(ends) == 2
    assert all(end.closed for end in ends)
