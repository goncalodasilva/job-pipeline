"""Logging setup with redaction (spec 2.9 rule 9 and 2.10; ADR-030).

Records are written as JSON lines to rotating files in the data folder's ``logs/`` and as
plain text to stderr. Before the data folder exists (a first run, ADR-044 item 3) the file is
replaced by a bounded in-memory buffer that is written out once the folder is attached.

Records that carry an exception from uvicorn's loggers (an unexpected error in a route) and
from the uncaught-exception hooks are reduced to level, logger name and exception type, because
the exception text and traceback can hold job text, notes or a fetched link (``ErrorRecordFilter``).
The ``httpx`` and ``httpcore`` loggers are dropped below WARNING and reduced from there, since
their records hold URLs and redirect targets (``ClientRecordFilter``, TD-228).

Records from the Claude Agent SDK's loggers can quote CLI output (prompt or answer text) and
exception text (which can hold a key echoed on the CLI's stderr), so they are reduced to level,
logger name and exception type as they are created (``install_sdk_record_reduction``, installed
by ``configure_logging``), before any handler sees them, including ones the app didn't add
(``logging.lastResort``, test capture). ``SdkRecordFilter`` on the app's handlers does the same
again as a second line of defence.

The redaction filter sits on the handlers, not on loggers, so records from every logger,
including uvicorn's, pass through it before they are written.

Limits: only the handlers added here are filtered, so ``configure_logging`` must run before any
other handler is installed, and uvicorn must be started with ``log_config=None`` (its default
config re-installs its own handlers, which would bypass the filter).
"""

import io
import json
import logging
import logging.handlers
import os
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from app.domain.redaction import Redactor

LOG_FILE_NAME = "jobpipeline.log"
MAX_LOG_BYTES = 1_048_576
LOG_BACKUP_COUNT = 4  # the live file plus 4 backups: 5 files in total
ACCESS_LOGGER = "uvicorn.access"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", ACCESS_LOGGER)
SDK_LOGGER = "claude_agent_sdk"
# The HTTP client libraries log URLs, hosts and redirect targets at DEBUG and INFO.
CLIENT_LOGGERS = ("httpx", "httpcore")
# Set on an SDK record once reduced, so a later pass (another handler's filter) keeps the
# exception type it can no longer see. Internal: never treated as an extra or written out.
SDK_REDUCED_ATTR = "jobpipeline_sdk_reduced"
# Set (via ``extra``) by the uncaught-exception hooks: their records are reduced like uvicorn's.
REDUCE_EXCEPTION_ATTR = "jobpipeline_reduce_exception"
_LOG = logging.getLogger("jobpipeline.logging")

# Attributes every LogRecord has; anything else on a record came from ``extra``.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
    SDK_REDUCED_ATTR,
    REDUCE_EXCEPTION_ATTR,
}

_PLAIN_TYPES = (int, float, bool, type(None))
_CORE_FIELDS = ("time", "level", "logger", "message", "exception", "stack")
# uvicorn adds a coloured copy of its format template; it is noise in a JSON log.
_DROPPED_EXTRAS = frozenset({"color_message"})


def _extras(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS}


class RedactionFilter(logging.Filter):
    """Rewrite a record so its message, extras, traceback and stack hold no secrets."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        redact = self._redactor.redact
        try:
            message = record.getMessage()
        except Exception:  # a bad format call must not raise at the call site
            message = f"unformattable log message: {record.msg!s} {record.args!r}"
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        # Formatters reuse exc_text; dropping exc_info stops any of them re-rendering the raw one.
        record.exc_info = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        for key, value in _extras(record).items():
            if not isinstance(value, _PLAIN_TYPES):
                setattr(record, key, redact(str(value)))
        return True


class AccessQueryFilter(logging.Filter):
    """Cut the query string from uvicorn's access records, keeping method, path and status.

    A GET form puts what the user typed in the address (a Library search, for one), and
    redaction only knows secrets, so the query is dropped whole (spec 2.10, ADR-093).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn's access arguments: client, method, path with query, HTTP version, status.
        if record.name == ACCESS_LOGGER and isinstance(args, tuple) and len(args) == 5:
            path = args[2]
            if isinstance(path, str):
                record.args = (*args[:2], path.partition("?")[0], *args[3:])
        return True


class SdkRecordFilter(logging.Filter):
    """Reduce records from the Claude Agent SDK to level, logger name and exception type.

    The SDK logs CLI stdout lines and error texts that can hold prompt or answer text
    (spec 3.3), which redaction can't recognise. Other records pass unchanged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if _is_sdk_record(record):
            _reduce_sdk_record(record)
        return True


class ErrorRecordFilter(logging.Filter):
    """Reduce a uvicorn record that carries an exception, and the uncaught-exception hooks'
    records, to level, logger name and exception type (spec 2.10).

    The exception message and traceback of an unexpected error in a route can quote job text,
    notes, removal reasons or a fetched link, which redaction can't recognise. The record is
    still written, so an error is visible; other uvicorn records (access, start-up) pass
    unchanged. A record already reduced by an earlier handler's filter is rebuilt the same.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if _is_error_source(record) and (
            record.exc_info or record.exc_text or hasattr(record, SDK_REDUCED_ATTR)
        ):
            _reduce_sdk_record(record)
        return True


class ClientRecordFilter(logging.Filter):
    """Drop ``httpx`` and ``httpcore`` records below WARNING and reduce the others to level,
    logger name and exception type: their texts hold URLs, hosts and ``Location`` headers (the
    fetched link is never logged; spec 2.10, TD-228)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _named_in(record, CLIENT_LOGGERS):
            return True
        if record.levelno < logging.WARNING:
            return False
        _reduce_sdk_record(record)
        return True


def _named_in(record: logging.LogRecord, roots: tuple[str, ...]) -> bool:
    name = record.name if isinstance(record.name, str) else ""
    return any(name == root or name.startswith(root + ".") for root in roots)


def _is_error_source(record: logging.LogRecord) -> bool:
    flagged = getattr(record, REDUCE_EXCEPTION_ATTR, False) is True
    return flagged or _named_in(record, UVICORN_LOGGERS)


def _is_sdk_record(record: logging.LogRecord) -> bool:
    name = record.name if isinstance(record.name, str) else ""
    return name == SDK_LOGGER or name.startswith(SDK_LOGGER + ".")


def _reduce_sdk_record(record: logging.LogRecord) -> None:
    """Keep only level, logger name and exception type: no message text, args, extras,
    exception text or stack.

    The message is always rebuilt. The exception type is stored on the record (``""`` for
    none), so a second pass, which no longer has ``exc_info``, still names it; a stored value
    that isn't a plain identifier is ignored, so a record that arrives already marked can't
    carry text through."""
    if record.exc_info and record.exc_info[0] is not None:
        error_type = record.exc_info[0].__name__
    else:
        stored = getattr(record, SDK_REDUCED_ATTR, "")
        error_type = stored if isinstance(stored, str) and stored.isidentifier() else ""
    setattr(record, SDK_REDUCED_ATTR, error_type)
    summary = f"{record.levelname} from {record.name}"
    record.msg = f"{summary} ({error_type})" if error_type else summary
    record.args = None
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    for key in _extras(record):
        delattr(record, key)


class _SdkReducingFactory:
    """A log record factory that reduces the SDK's records as they are created and passes
    every other record through ``inner`` unchanged."""

    def __init__(self, inner: Callable[..., logging.LogRecord]) -> None:
        self.inner = inner

    def __call__(self, *args: object, **kwargs: object) -> logging.LogRecord:
        record = self.inner(*args, **kwargs)
        if _is_sdk_record(record):
            _reduce_sdk_record(record)
        return record


def install_sdk_record_reduction() -> Callable[[], None]:
    """Reduce every record from the Claude Agent SDK's loggers (``claude_agent_sdk`` and its
    children) at creation, so no handler, filter or capture ever sees its text.

    A filter on the ``claude_agent_sdk`` logger would miss its child loggers' records, so this
    wraps the log record factory instead. ``extra`` fields are set by ``Logger.makeRecord``
    after the factory runs, so they are left to ``SdkRecordFilter`` on the app's handlers (the
    SDK passes none). Installing again while installed changes nothing. Returns a function that
    puts the previous factory back if this one is still in place (a factory installed on top
    later is left alone, and keeps this one inside it); calling it twice is safe.
    """
    current = logging.getLogRecordFactory()
    if isinstance(current, _SdkReducingFactory):
        return lambda: None
    factory = _SdkReducingFactory(current)
    logging.setLogRecordFactory(factory)

    def restore() -> None:
        if logging.getLogRecordFactory() is factory:
            logging.setLogRecordFactory(current)

    return restore


def _add_filters(handler: logging.Handler, redactor: Redactor) -> None:
    handler.addFilter(SdkRecordFilter())
    handler.addFilter(AccessQueryFilter())
    handler.addFilter(ErrorRecordFilter())
    handler.addFilter(ClientRecordFilter())
    handler.addFilter(RedactionFilter(redactor))


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, extras and exception."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            k: v
            for k, v in _extras(record).items()
            if k not in _DROPPED_EXTRAS and k not in _CORE_FIELDS
        }
        payload.update(
            time=datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            level=record.levelname,
            logger=record.name,
            message=record.getMessage(),
        )
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            payload["exception"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info
        return json.dumps(payload, ensure_ascii=False, default=str)


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating log file readable only by its owner; logs can hold personal data."""

    def _open(self) -> io.TextIOWrapper:
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


_TRACEBACK_START = "Traceback (most recent call last)"


class ConsoleFormatter(logging.Formatter):
    """One plain line per record; tracebacks go only to the log file.

    Some libraries put a traceback in the message text itself (uvicorn does on a failed
    start-up), so the console also cuts the message where a traceback begins.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage().split(_TRACEBACK_START, 1)[0].strip()
        first_line = message.splitlines()[0] if message else "details are in the log file"
        return f"{record.levelname} {record.name}: {first_line}"


class _BufferHandler(logging.Handler):
    """Keeps the most recent records in memory until the log folder exists (ADR-044 item 3).

    Its redaction filter runs before a record is stored, and the file handler's filter runs
    again when the record is written out, so a secret registered in between is caught too.
    Once handed over, any record still arriving (a thread that was waiting for the lock while
    the buffer was replaced) is forwarded to the file handler instead of being lost.
    """

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._records: deque[logging.LogRecord] = deque(maxlen=limit)
        self._dropped = 0
        self._target: logging.Handler | None = None

    def emit(self, record: logging.LogRecord) -> None:
        if self._target is not None:
            self._target.handle(record)
            return
        if len(self._records) == self._records.maxlen:
            self._dropped += 1
        self._records.append(record)

    def hand_over(self, target: logging.Handler) -> None:
        """Write the dropped-records note and the stored records to ``target``, oldest first,
        and forward later records to it (call with the lock held)."""
        if self._dropped:
            note = _LOG.makeRecord(
                _LOG.name,
                logging.INFO,
                __file__,
                0,
                "%d earlier log records were dropped",
                (self._dropped,),
                None,
            )
            target.handle(note)
        for record in self._records:
            target.handle(record)
        self._records.clear()
        self._dropped = 0
        self._target = target

    def stop_forwarding(self) -> None:
        """Stop forwarding (call with the lock held), so nothing reopens a closed file."""
        self._target = None


class LoggingSetup:
    """The handlers ``configure_logging`` added, so a log folder can be attached later and
    every handler removed again."""

    def __init__(
        self,
        redactor: Redactor,
        console: logging.Handler,
        *,
        file_handler: logging.Handler | None = None,
        buffer: _BufferHandler | None = None,
        restore_record_factory: Callable[[], None] | None = None,
    ) -> None:
        self._redactor = redactor
        self._console = console
        self._file_handler = file_handler
        self._buffer = buffer
        self._restore_record_factory = restore_record_factory
        self._lock = threading.Lock()

    def attach_log_folder(self, log_dir: Path) -> None:
        """Start writing the log file in ``log_dir``, first the buffered records in order.

        Raises ``RuntimeError`` when a log folder is already attached.
        """
        with self._lock:
            if self._file_handler is not None:
                raise RuntimeError("a log folder is already attached")
            file_handler = _file_handler(log_dir, self._redactor)
            root = logging.getLogger()
            buffer = self._buffer
            if buffer is None:
                root.addHandler(file_handler)
            else:
                # Under the buffer's lock no record lands in the buffer while it is replaced.
                with _handler_lock(buffer):
                    buffer.hand_over(file_handler)
                    root.addHandler(file_handler)
                    root.removeHandler(buffer)
                # Kept until close(), which stops its forwarding before the file closes.
            self._file_handler = file_handler

    def detach_log_folder(self) -> None:
        """Stop writing the log file and close it (delete all data removes the folder), so
        ``attach_log_folder`` can be called again. Records go to the console only until then.
        Does nothing when no log folder is attached."""
        root = logging.getLogger()
        with self._lock:
            handler, self._file_handler = self._file_handler, None
            if handler is None:
                return
            buffer, self._buffer = self._buffer, None
            if buffer is not None:
                with _handler_lock(buffer):
                    buffer.stop_forwarding()  # nothing reopens the file being closed
            root.removeHandler(handler)
            handler.close()

    def close(self) -> None:
        root = logging.getLogger()
        with self._lock:
            if self._buffer is not None:
                with _handler_lock(self._buffer):
                    self._buffer.stop_forwarding()
            handlers = (self._buffer, self._file_handler, self._console)
            for handler in handlers:
                if handler is not None:
                    root.removeHandler(handler)
                    handler.close()
            self._file_handler = self._buffer = None
            restore_record_factory, self._restore_record_factory = (
                self._restore_record_factory,
                None,
            )
        logging.captureWarnings(False)
        if restore_record_factory is not None:
            restore_record_factory()


@contextmanager
def _handler_lock(handler: logging.Handler) -> Iterator[None]:
    handler.acquire()
    try:
        yield
    finally:
        handler.release()


def _file_handler(log_dir: Path, redactor: Redactor) -> logging.Handler:
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(log_dir, 0o700)
    handler = PrivateRotatingFileHandler(
        log_dir / LOG_FILE_NAME,
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(JsonFormatter())
    _add_filters(handler, redactor)
    return handler


def configure_logging(
    log_dir: Path | None, level: str, redactor: Redactor, *, buffer_limit: int = 1000
) -> LoggingSetup:
    """Install redacted console logging plus a log file in ``log_dir``.

    Without a folder (before the workspace exists) the most recent ``buffer_limit`` records
    are kept in memory instead, and nothing is written to disk until
    ``LoggingSetup.attach_log_folder`` is called.
    """
    file_handler = _file_handler(log_dir, redactor) if log_dir is not None else None
    buffer = _BufferHandler(buffer_limit) if log_dir is None else None
    if buffer is not None:
        _add_filters(buffer, redactor)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ConsoleFormatter())
    _add_filters(console, redactor)

    root = logging.getLogger()
    for handler in (file_handler or buffer, console):
        if handler is not None:
            root.addHandler(handler)
    root.setLevel(level)
    logging.captureWarnings(True)
    restore_record_factory = install_sdk_record_reduction()

    # uvicorn installs its own handlers; route its records through ours instead.
    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    return LoggingSetup(
        redactor,
        console,
        file_handler=file_handler,
        buffer=buffer,
        restore_record_factory=restore_record_factory,
    )


_REDUCE_EXTRA: dict[str, object] = {REDUCE_EXCEPTION_ATTR: True}


def install_exception_hooks(logger_name: str = "jobpipeline") -> Callable[[], None]:
    """Log uncaught exceptions (main and other threads) instead of printing them raw.

    Returns a function that puts the previous hooks back.
    """
    logger = logging.getLogger(logger_name)
    previous_sys, previous_thread = sys.excepthook, threading.excepthook

    def log_uncaught(
        exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        logger.critical("Uncaught exception", exc_info=(exc_type, exc, tb), extra=_REDUCE_EXTRA)

    def log_thread(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is None:
            return
        name = args.thread.name if args.thread is not None else "unknown"
        logger.critical(
            "Uncaught exception in thread %s",
            name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            extra=_REDUCE_EXTRA,
        )

    sys.excepthook = log_uncaught
    threading.excepthook = log_thread

    def restore() -> None:
        sys.excepthook, threading.excepthook = previous_sys, previous_thread

    return restore
