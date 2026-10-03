"""Logging setup with redaction (spec 2.9 rule 9 and 2.10; ADR-030).

Records are written as JSON lines to rotating files in the data folder's ``logs/`` and as
plain text to stderr. The redaction filter sits on the handlers, not on loggers, so records
from every logger, including uvicorn's, pass through it before they are written.

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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from app.domain.redaction import Redactor

LOG_FILE_NAME = "jobpipeline.log"
MAX_LOG_BYTES = 1_048_576
LOG_BACKUP_COUNT = 4  # the live file plus 4 backups: 5 files in total
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Attributes every LogRecord has; anything else on a record came from ``extra``.
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
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


@dataclass(frozen=True)
class LoggingSetup:
    """The handlers ``configure_logging`` added, so they can be removed again."""

    handlers: tuple[logging.Handler, ...]

    def close(self) -> None:
        root = logging.getLogger()
        for handler in self.handlers:
            root.removeHandler(handler)
            handler.close()
        logging.captureWarnings(False)


def configure_logging(log_dir: Path, level: str, redactor: Redactor) -> LoggingSetup:
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(log_dir, 0o700)
    file_handler = PrivateRotatingFileHandler(
        log_dir / LOG_FILE_NAME,
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(ConsoleFormatter())

    root = logging.getLogger()
    for handler in (file_handler, stderr_handler):
        handler.addFilter(RedactionFilter(redactor))
        root.addHandler(handler)
    root.setLevel(level)
    logging.captureWarnings(True)

    # uvicorn installs its own handlers; route its records through ours instead.
    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    return LoggingSetup(handlers=(file_handler, stderr_handler))


def install_exception_hooks(logger_name: str = "jobpipeline") -> Callable[[], None]:
    """Log uncaught exceptions (main and other threads) instead of printing them raw.

    Returns a function that puts the previous hooks back.
    """
    logger = logging.getLogger(logger_name)
    previous_sys, previous_thread = sys.excepthook, threading.excepthook

    def log_uncaught(
        exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        logger.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def log_thread(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is None:
            return
        name = args.thread.name if args.thread is not None else "unknown"
        logger.critical(
            "Uncaught exception in thread %s",
            name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = log_uncaught
    threading.excepthook = log_thread

    def restore() -> None:
        sys.excepthook, threading.excepthook = previous_sys, previous_thread

    return restore
