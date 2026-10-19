"""Run a text extractor in a child process with a deadline (spec 2.9 rule 5).

A parser works on a file somebody else made, so it runs outside the app's process: a hang is
killed at the deadline and a crash only ends the child. The child answers with plain bytes (a
status byte, then the text or an error kind), never a pickled object, and the parent maps the
kind through a fixed list. A parser's own error text can quote the document, so it is dropped
in the child and never crosses over. The child also starts with an empty environment, a limit
on its CPU time and no standard error.
"""

import contextlib
import logging
import math
import multiprocessing
import os
import sys
import threading
import time
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Final

from app.domain.errors import (
    AppError,
    EncryptedDocument,
    NoTextFound,
    ParseTimedOut,
    UnreadableDocument,
    UnsupportedDocumentType,
)
from app.modules.extract.registry import KINDS, TextExtractor, extractor_for

_TEXT: Final = b"T"
_ERROR: Final = b"E"
# Errors an extractor may report; anything else reads as an unreadable document.
_REPORTED: Final = (EncryptedDocument, NoTextFound, UnreadableDocument)
# What the child says instead of sending an answer longer than the cap.
_TOO_LONG: Final = b"AnswerTooLong"
# Above the 10 MB upload cap, so the text of any accepted text file fits; a larger answer is
# refused by the child instead of filling memory.
_MAX_ANSWER_BYTES: Final = 16 * 1024 * 1024
# CPU time the child may use past the deadline before the system ends it.
_CPU_MARGIN_SECONDS: Final = 5
# How long a child that has answered may take to exit before it is killed.
_EXIT_GRACE_SECONDS: Final = 5.0


def extract_text(
    path: Path, kind: str, timeout: float, *, extractor: TextExtractor | None = None
) -> str:
    """The text of the stored upload at ``path``, read as ``kind`` ("pdf", "docx" or "text").

    Raises ``UnreadableDocument`` (``EncryptedDocument`` for a password-protected PDF),
    ``NoTextFound`` or, after ``timeout`` seconds, ``ParseTimedOut``. ``extractor`` replaces
    the parser chosen by ``kind`` and must be picklable. Blocks until the child has ended, so
    call it from a worker thread, not the event loop."""
    if kind not in KINDS:
        raise UnsupportedDocumentType(detail="unknown document kind")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(
        target=_parse_in_child, args=(sender, path, kind, extractor, timeout), daemon=True
    )
    deadline = time.monotonic() + timeout
    try:
        child.start()
    except Exception as error:  # no process, or an extractor that can't be sent to one
        receiver.close()
        raise UnreadableDocument(detail=type(error).__name__) from None
    finally:
        sender.close()  # the child holds the only open end, so its death ends the wait
    # Killing the child closes its end of the pipe, which also ends a half-sent answer.
    watchdog = threading.Timer(timeout, child.kill)
    watchdog.start()
    try:
        answer = _receive(receiver)
        expired = time.monotonic() >= deadline
    finally:
        watchdog.cancel()
        watchdog.join()
        receiver.close()
        _reap(child)
    if answer is None:
        if expired:
            raise ParseTimedOut(detail="the parser was stopped at the deadline")
        raise UnreadableDocument(detail="the parser ended without an answer")
    return _result(answer)


def _parse_in_child(
    sender: Connection, path: Path, kind: str, extractor: TextExtractor | None, timeout: float
) -> None:
    os.environ.clear()  # the parser never sees what the app inherited, secrets included
    _limit_cpu_time(timeout)
    _silence_stderr()
    logging.disable(logging.CRITICAL)  # a parser's warnings can quote the document
    try:
        text = (extractor or extractor_for(kind)).extract(path)
        answer = _TEXT + text.encode("utf-8")
    except _REPORTED as error:
        answer = _ERROR + error.kind.encode("ascii")
    except Exception:
        answer = _ERROR + UnreadableDocument.kind.encode("ascii")
    if len(answer) > _MAX_ANSWER_BYTES:
        answer = _ERROR + _TOO_LONG
    with contextlib.suppress(OSError):  # the parent stopped listening; nobody to tell
        sender.send_bytes(answer)
    sender.close()


def _cpu_limit_seconds(timeout: float) -> int:
    return math.ceil(timeout) + _CPU_MARGIN_SECONDS


def _limit_cpu_time(timeout: float) -> None:
    """Have the system end the child if it keeps computing after the parent was killed and
    its deadline with it. Only where the platform has such a limit."""
    if sys.platform == "win32":
        return
    import resource

    try:
        ceiling = resource.getrlimit(resource.RLIMIT_CPU)[1]
        seconds = _cpu_limit_seconds(timeout)
        if ceiling != resource.RLIM_INFINITY:
            seconds = min(seconds, ceiling)
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
    except (ValueError, OSError):
        pass  # the deadline in the parent still applies


def _silence_stderr() -> None:
    """Send the child's standard error nowhere: a library's warning or traceback can quote the
    document."""
    try:
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 2)
        os.close(null)
    except OSError:
        pass


def _receive(receiver: Connection) -> bytes | None:
    """The child's answer, or None when it ended (or was killed) without a usable one."""
    try:
        return receiver.recv_bytes(_MAX_ANSWER_BYTES)
    except (EOFError, OSError):
        return None


def _reap(child: BaseProcess) -> None:
    """Wait for the child so no process or zombie is left; kill it if it lingers."""
    child.join(_EXIT_GRACE_SECONDS)
    if child.is_alive():
        child.kill()
        child.join()
    child.close()


def _result(answer: bytes) -> str:
    status, body = answer[:1], answer[1:]
    if status == _TEXT:
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            raise UnreadableDocument(detail="the parser's answer was not text") from None
    raise _reported_error(body)


def _reported_error(kind: bytes) -> AppError:
    if kind == _TOO_LONG:
        return UnreadableDocument(detail="the parser's answer passed the cap")
    for cls in _REPORTED:
        if cls.kind.encode("ascii") == kind:
            return cls()
    return UnreadableDocument(detail="the parser's answer was not understood")
