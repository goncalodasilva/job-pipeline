"""Logging with redaction (task M0.10; AC-M0.5; spec 2.9 rule 9 and 2.10; ADR-030).

Every test writes logs into ``tmp_path`` only. ``configure_logging`` changes the root logger,
so the ``logging_setup`` fixture closes what it added and restores the root and uvicorn
logger state afterwards. Key-shaped strings are built at runtime from parts, never written as
literals. Every value here is fictional.
"""

import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.errors import ProviderAuthError
from app.domain.redaction import REDACTED, Redactor
from app.logging_config import (
    LOG_BACKUP_COUNT,
    LOG_FILE_NAME,
    MAX_LOG_BYTES,
    SDK_REDUCED_ATTR,
    JsonFormatter,
    LoggingSetup,
    RedactionFilter,
    SdkRecordFilter,
    configure_logging,
    install_sdk_record_reduction,
)

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
FICTIONAL_HIDDEN = "FictionalHiddenValue-0042-notreal"
FICTIONAL_BODY = "FICTIONALtestOnly0123456789_abc-XYZ"


def anthropic_shaped(kind: str, body: str = FICTIONAL_BODY) -> str:
    """Build an Anthropic-shaped token (``sk-ant-<kind>-<body>``) at runtime."""
    return "-".join(["sk", "ant", kind, body])


# --- fixtures and helpers ------------------------------------------------------------------


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Save and restore root and uvicorn logger state around a test."""
    root = logging.getLogger()
    saved_root = (list(root.handlers), list(root.filters), root.level)
    saved_uvicorn = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in UVICORN_LOGGERS
    }
    yield
    root.handlers[:] = saved_root[0]
    root.filters[:] = saved_root[1]
    root.setLevel(saved_root[2])
    for name, (handlers, propagate, level) in saved_uvicorn.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.propagate = propagate
        logger.setLevel(level)


@pytest.fixture
def redactor() -> Redactor:
    instance = Redactor()
    instance.register(FICTIONAL_HIDDEN)
    return instance


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "logs"


@pytest.fixture
def make_logging(
    log_dir: Path,
    redactor: Redactor,
    restore_logging: None,
    capsys: pytest.CaptureFixture[str],
) -> Iterator[Callable[..., LoggingSetup]]:
    """Return a factory that configures logging; every setup made is closed afterwards."""
    setups: list[LoggingSetup] = []

    def factory(level: str = "DEBUG") -> LoggingSetup:
        setup = configure_logging(log_dir, level, redactor)
        setups.append(setup)
        return setup

    yield factory
    for setup in setups:
        setup.close()


def flush_root() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def read_log_text(log_dir: Path) -> str:
    flush_root()
    return (log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")


def read_log_records(log_dir: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in read_log_text(log_dir).splitlines() if line.strip()]


def make_record(
    msg: str,
    args: tuple[object, ...] = (),
    *,
    exc_info: object = None,
    created: float | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="jobpipeline.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,  # type: ignore[arg-type]
    )
    if created is not None:
        record.created = created
        record.msecs = (created - int(created)) * 1000
    return record


# --- AC-M0.5: registered secrets ------------------------------------------------------------


def test_ac_m0_5_registered_secret_redacted_in_message_args_extra_and_traceback(
    make_logging: Callable[..., LoggingSetup],
    log_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_logging("DEBUG")
    logger = logging.getLogger("jobpipeline.test.redaction")

    logger.info(f"inline {FICTIONAL_HIDDEN} in the message")
    logger.info("via args %s and %r", FICTIONAL_HIDDEN, FICTIONAL_HIDDEN)
    logger.info("via extra", extra={"provider_hint": f"key={FICTIONAL_HIDDEN}"})
    try:
        raise ProviderAuthError(detail=f"rejected credential {FICTIONAL_HIDDEN}")
    except ProviderAuthError:
        logger.exception("provider call failed")
    try:
        raise RuntimeError(f"plain error with {FICTIONAL_HIDDEN}")
    except RuntimeError:
        logger.error("plain failure", exc_info=True)

    text = read_log_text(log_dir)
    err = capsys.readouterr().err

    assert FICTIONAL_HIDDEN not in text
    assert FICTIONAL_HIDDEN not in err
    records = read_log_records(log_dir)
    assert len(records) == 5
    assert all(REDACTED in str(record["message"]) for record in records[:2])
    assert REDACTED in str(records[2]["provider_hint"])
    assert "ProviderAuthError" in str(records[3]["exception"])
    assert REDACTED in str(records[3]["exception"])
    assert REDACTED in str(records[4]["exception"])
    assert REDACTED in err


def test_redaction_filter_rewrites_record_and_keeps_it(redactor: Redactor) -> None:
    redaction_filter = RedactionFilter(redactor)
    record = make_record("value %s", (FICTIONAL_HIDDEN,))
    record.stack_info = f"Stack (most recent call last):\n  frame with {FICTIONAL_HIDDEN}"
    setattr(record, "provider_hint", FICTIONAL_HIDDEN)  # noqa: B010

    kept = redaction_filter.filter(record)

    assert kept is True
    assert record.getMessage() == f"value {REDACTED}"
    assert FICTIONAL_HIDDEN not in str(record.stack_info)
    assert getattr(record, "provider_hint") == REDACTED  # noqa: B009
    assert FICTIONAL_HIDDEN not in JsonFormatter().format(record)


def test_redaction_filter_redacts_exception_text(redactor: Redactor) -> None:
    redaction_filter = RedactionFilter(redactor)
    try:
        raise ProviderAuthError(detail=f"rejected credential {FICTIONAL_HIDDEN}")
    except ProviderAuthError:
        record = make_record("failed", exc_info=sys.exc_info())

    redaction_filter.filter(record)

    assert record.exc_text is not None
    assert FICTIONAL_HIDDEN not in record.exc_text
    assert REDACTED in record.exc_text
    assert FICTIONAL_HIDDEN not in JsonFormatter().format(record)
    assert FICTIONAL_HIDDEN not in logging.Formatter().format(record)


# --- AC-M0.5: token shapes ------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["api03", "oat01"])
def test_ac_m0_5_anthropic_key_and_oauth_shapes_redacted(
    kind: str,
    make_logging: Callable[..., LoggingSetup],
    log_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_logging("DEBUG")
    token = anthropic_shaped(kind)  # never registered: the shape alone must be caught
    logger = logging.getLogger("jobpipeline.test.shapes")

    logger.warning("header was %s", f"Bearer {token}")
    logger.warning("extra too", extra={"auth": token})

    text = read_log_text(log_dir)
    err = capsys.readouterr().err
    assert token not in text
    assert FICTIONAL_BODY not in text
    assert token not in err
    assert REDACTED in text


# --- AC-M0.5: property ----------------------------------------------------------------------


@given(
    secret=st.text(
        alphabet=st.characters(categories=["Lu", "Ll", "Nd"], max_codepoint=127),
        min_size=9,  # longer than "REDACTED", so it can't hide inside the marker
        max_size=40,
    ),
    before=st.text(max_size=60),
    after=st.text(max_size=60),
)
def test_ac_m0_5_secret_embedded_in_arbitrary_text_redacted(
    secret: str, before: str, after: str
) -> None:
    instance = Redactor()
    instance.register(secret)
    redaction_filter = RedactionFilter(instance)
    record = make_record("%s", (before + secret + after,))

    redaction_filter.filter(record)
    output = JsonFormatter().format(record)

    assert secret not in instance.redact(before + secret + after)
    assert secret not in record.getMessage()
    assert secret not in json.loads(output)["message"]
    assert REDACTED in record.getMessage()


# --- AC-M0.5: uvicorn ------------------------------------------------------------------------


def test_ac_m0_5_uvicorn_access_log_redacted(
    make_logging: Callable[..., LoggingSetup],
    log_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    access = logging.getLogger("uvicorn.access")
    stray = logging.StreamHandler(sys.stderr)
    access.addHandler(stray)  # uvicorn installs its own handlers; they must be dropped
    access.propagate = False

    make_logging("INFO")
    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        assert logger.handlers == []
        assert logger.propagate is True

    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:50123",
        "GET",
        f"/setup/callback?token={FICTIONAL_HIDDEN}",
        "1.1",
        200,
    )
    logging.getLogger("uvicorn.error").info("startup with %s", FICTIONAL_HIDDEN)

    text = read_log_text(log_dir)
    err = capsys.readouterr().err
    assert FICTIONAL_HIDDEN not in text
    assert FICTIONAL_HIDDEN not in err
    records = read_log_records(log_dir)
    access_records = [r for r in records if r["logger"] == "uvicorn.access"]
    assert len(access_records) == 1
    # The query string is cut from access records whole (ADR-093), so the secret isn't even
    # there to redact; method, path and status stay.
    message = str(access_records[0]["message"])
    assert '"GET /setup/callback HTTP/1.1" 200' in message
    assert "?" not in message
    assert "token" not in message


def test_uvicorn_access_log_drops_the_query_string(
    make_logging: Callable[..., LoggingSetup], log_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_logging("INFO")
    access = logging.getLogger("uvicorn.access")

    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:50123",
        "GET",
        "/facts?tab=library&q=fictional-search-words",
        "1.1",
        200,
    )
    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:50123", "GET", "/health", "1.1", 200)
    logging.getLogger("uvicorn.error").info("kept as written: %s", "/facts?tab=library")

    text = read_log_text(log_dir)
    assert "fictional-search-words" not in text
    assert "fictional-search-words" not in capsys.readouterr().err
    messages = [str(r["message"]) for r in read_log_records(log_dir)]
    assert any('"GET /facts HTTP/1.1" 200' in m for m in messages)
    assert any('"GET /health HTTP/1.1" 200' in m for m in messages)
    # Only access records are cut; other loggers' text is left to redaction.
    assert any("/facts?tab=library" in m for m in messages)


# --- handlers, levels and lifecycle ----------------------------------------------------------


def test_configure_logging_creates_dir_and_filters_on_handlers(
    make_logging: Callable[..., LoggingSetup], log_dir: Path
) -> None:
    root = logging.getLogger()
    before = list(root.handlers)

    make_logging("INFO")

    assert log_dir.is_dir()
    added = [h for h in root.handlers if h not in before]
    assert len(added) == 2
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in added)
    assert any(type(h) is logging.StreamHandler and h.stream is sys.stderr for h in added)
    for handler in added:
        assert any(isinstance(f, RedactionFilter) for f in handler.filters)
    assert not any(isinstance(f, RedactionFilter) for f in root.filters)
    assert root.level == logging.INFO


def test_level_drops_lower_records(
    make_logging: Callable[..., LoggingSetup], log_dir: Path
) -> None:
    make_logging("WARNING")
    logger = logging.getLogger("jobpipeline.test.level")

    logger.info("dropped info")
    logger.warning("kept warning")

    messages = [r["message"] for r in read_log_records(log_dir)]
    assert messages == ["kept warning"]


def test_close_removes_and_closes_added_handlers(
    make_logging: Callable[..., LoggingSetup],
) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    setup = make_logging("INFO")
    added = [h for h in root.handlers if h not in before]

    setup.close()

    assert root.handlers == before
    file_handlers = [h for h in added if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert file_handlers
    assert all(h.stream is None for h in file_handlers)


# --- JSON formatter ---------------------------------------------------------------------------


def test_json_formatter_fields() -> None:
    created = 1_790_000_000.25
    record = make_record("hello %s", ("world",), created=created)
    setattr(record, "job_id", "job-0001")  # noqa: B010

    line = JsonFormatter().format(record)

    assert "\n" not in line
    payload = json.loads(line)
    assert payload["message"] == "hello world"
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "jobpipeline.test"
    assert payload["job_id"] == "job-0001"
    assert "exception" not in payload
    stamp = datetime.fromisoformat(payload["time"])
    assert stamp.utcoffset() is not None
    assert stamp.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert stamp == datetime.fromtimestamp(created, tz=UTC)


def test_json_formatter_includes_exception_on_one_line() -> None:
    try:
        raise ValueError("fictional failure")
    except ValueError:
        record = make_record("boom", exc_info=sys.exc_info())

    line = JsonFormatter().format(record)

    assert "\n" not in line
    payload = json.loads(line)
    assert "ValueError: fictional failure" in payload["exception"]
    assert "Traceback" in payload["exception"]


# --- rotation -----------------------------------------------------------------------------------


def test_rotation_constants() -> None:
    assert LOG_FILE_NAME == "jobpipeline.log"
    assert MAX_LOG_BYTES == 1_048_576
    assert LOG_BACKUP_COUNT == 4  # live file plus 4 backups = 5 files


def test_rotation_1mb_5_files(make_logging: Callable[..., LoggingSetup], log_dir: Path) -> None:
    make_logging("DEBUG")
    logger = logging.getLogger("jobpipeline.test.rotation")
    payload = "x" * 2000
    record_count = (6 * MAX_LOG_BYTES) // len(payload)

    for index in range(record_count):
        logger.debug("record %d %s", index, payload)
    flush_root()

    files = sorted(log_dir.glob(f"{LOG_FILE_NAME}*"))
    assert len(files) == 5
    slack = len(payload) + 512
    for path in files:
        assert path.stat().st_size <= MAX_LOG_BYTES + slack
    last = read_log_records(log_dir)[-1]
    assert last["message"] == f"record {record_count - 1} {payload}"


# --- review regressions -------------------------------------------------------------------------


def test_bad_format_call_does_not_raise_and_stays_redacted(redactor: Redactor) -> None:
    record = make_record("%s %s", (FICTIONAL_HIDDEN,))  # one argument short

    kept = RedactionFilter(redactor).filter(record)

    assert kept is True
    assert FICTIONAL_HIDDEN not in record.getMessage()
    assert REDACTED in record.getMessage()


def test_non_string_message_with_secret_is_redacted(redactor: Redactor) -> None:
    record = make_record("unused")
    record.msg = {"credential": FICTIONAL_HIDDEN}  # type: ignore[assignment]

    RedactionFilter(redactor).filter(record)

    assert FICTIONAL_HIDDEN not in record.getMessage()


def test_extras_keep_plain_types_and_redact_containers(redactor: Redactor) -> None:
    record = make_record("extras")
    for key, value in {
        "count": 3,
        "ok": True,
        "missing": None,
        "hints": [FICTIONAL_HIDDEN],
        "nested": {"key": FICTIONAL_HIDDEN},
    }.items():
        setattr(record, key, value)

    RedactionFilter(redactor).filter(record)
    payload = json.loads(JsonFormatter().format(record))

    assert (payload["count"], payload["ok"], payload["missing"]) == (3, True, None)
    assert FICTIONAL_HIDDEN not in json.dumps(payload)
    assert REDACTED in payload["hints"]
    assert REDACTED in payload["nested"]


def test_extras_cannot_overwrite_core_fields() -> None:
    record = make_record("real message")
    for key in ("time", "level", "logger", "message"):
        setattr(record, key, "forged")

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "jobpipeline.test"
    assert payload["message"] == "real message"
    assert payload["time"] != "forged"


def test_uvicorn_color_message_extra_is_dropped() -> None:
    record = make_record("Started server process [%d]", (42,))
    setattr(record, "color_message", "Started server process [%d]")  # noqa: B010

    payload = json.loads(JsonFormatter().format(record))

    assert "color_message" not in payload


def test_filtered_record_cannot_rerender_the_raw_traceback(redactor: Redactor) -> None:
    try:
        raise ProviderAuthError(detail=f"rejected credential {FICTIONAL_HIDDEN}")
    except ProviderAuthError:
        record = make_record("failed", exc_info=sys.exc_info())

    RedactionFilter(redactor).filter(record)

    assert record.exc_info is None
    assert REDACTED in str(record.exc_text)
    assert FICTIONAL_HIDDEN not in logging.Formatter().format(record)
    assert REDACTED in json.loads(JsonFormatter().format(record))["exception"]


def test_python_warnings_are_logged_and_redacted(
    make_logging: Callable[..., LoggingSetup], log_dir: Path
) -> None:
    import warnings

    make_logging("INFO")
    warnings.warn(f"deprecated option with {FICTIONAL_HIDDEN}", UserWarning, stacklevel=1)

    text = read_log_text(log_dir)
    assert "py.warnings" in text
    assert FICTIONAL_HIDDEN not in text


def test_log_folder_and_files_are_private_on_posix(
    make_logging: Callable[..., LoggingSetup], log_dir: Path
) -> None:
    import stat

    make_logging("DEBUG")
    logger = logging.getLogger("jobpipeline.test.private")
    for index in range((MAX_LOG_BYTES // 2000) + 50):  # force one rollover
        logger.debug("record %d %s", index, "y" * 2000)
    flush_root()

    if os.name != "posix":  # Windows has no owner-only mode bits; ACLs protect the folder
        return
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    files = sorted(log_dir.glob(f"{LOG_FILE_NAME}*"))
    assert len(files) >= 2
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --- console output and exception hooks (M0.15) -----------------------------------------------


def test_console_omits_traceback_but_file_keeps_it(
    make_logging: Callable[..., LoggingSetup],
    log_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_logging("INFO")
    logger = logging.getLogger("jobpipeline.test.console")

    try:
        raise RuntimeError(f"fictional startup failure {FICTIONAL_HIDDEN}")
    except RuntimeError:
        logger.exception("startup failed")

    records = read_log_records(log_dir)
    err = capsys.readouterr().err
    assert "Traceback" in str(records[-1]["exception"])
    assert "fictional startup failure" in str(records[-1]["exception"])
    assert "startup failed" in err
    assert "Traceback" not in err
    assert FICTIONAL_HIDDEN not in err


@pytest.fixture
def restore_hooks() -> Iterator[None]:
    saved = (sys.excepthook, threading.excepthook)
    yield
    sys.excepthook, threading.excepthook = saved


def test_install_exception_hooks_routes_sys_excepthook_to_logger(
    restore_hooks: None, caplog: pytest.LogCaptureFixture
) -> None:
    from app.logging_config import install_exception_hooks

    restore = install_exception_hooks("jobpipeline.test.hooks")
    try:
        raise ValueError("fictional uncaught error")
    except ValueError:
        exc_type, exc_value, exc_tb = sys.exc_info()
    with caplog.at_level(logging.DEBUG, logger="jobpipeline.test.hooks"):
        assert exc_type is not None and exc_value is not None
        sys.excepthook(exc_type, exc_value, exc_tb)
    restore()

    records = [r for r in caplog.records if r.name == "jobpipeline.test.hooks"]
    assert len(records) == 1
    assert records[0].levelno == logging.CRITICAL
    assert records[0].exc_info is not None
    assert records[0].exc_info[1] is exc_value


def test_install_exception_hooks_routes_thread_excepthook_to_logger(
    restore_hooks: None, caplog: pytest.LogCaptureFixture
) -> None:
    from app.logging_config import install_exception_hooks

    restore = install_exception_hooks("jobpipeline.test.hooks")

    def fail() -> None:
        raise ValueError("fictional thread error")

    with caplog.at_level(logging.DEBUG, logger="jobpipeline.test.hooks"):
        worker = threading.Thread(target=fail, name="fictional-worker")
        worker.start()
        worker.join(5.0)
    restore()

    records = [r for r in caplog.records if r.name == "jobpipeline.test.hooks"]
    assert len(records) == 1
    assert records[0].levelno == logging.CRITICAL
    assert records[0].exc_info is not None
    assert isinstance(records[0].exc_info[1], ValueError)


def test_install_exception_hooks_default_logger_and_restore(restore_hooks: None) -> None:
    from app.logging_config import install_exception_hooks

    before = (sys.excepthook, threading.excepthook)

    restore = install_exception_hooks()

    assert sys.excepthook is not before[0]
    assert threading.excepthook is not before[1]
    restore()
    assert (sys.excepthook, threading.excepthook) == before


# --- buffered logging before the data folder exists (M1.5; ADR-044 item 3) --------------------


@pytest.fixture
def make_buffered_logging(
    redactor: Redactor, restore_logging: None
) -> Iterator[Callable[..., LoggingSetup]]:
    """Configure logging without a folder (in-memory buffer); closed afterwards."""
    setups: list[LoggingSetup] = []

    def factory(level: str = "DEBUG", **kwargs: int) -> LoggingSetup:
        setup = configure_logging(None, level, redactor, **kwargs)
        setups.append(setup)
        return setup

    yield factory
    for setup in setups:
        setup.close()


def test_buffered_logging_creates_no_files(
    make_buffered_logging: Callable[..., LoggingSetup],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = logging.getLogger()
    before = list(root.handlers)

    make_buffered_logging("INFO")
    logging.getLogger("jobpipeline.test.buffer").warning("buffered with %s", FICTIONAL_HIDDEN)
    flush_root()

    assert list(tmp_path.iterdir()) == []
    added = [h for h in root.handlers if h not in before]
    assert not any(isinstance(h, logging.FileHandler) for h in added)
    assert any(type(h) is logging.StreamHandler and h.stream is sys.stderr for h in added)
    for handler in added:
        assert any(isinstance(f, RedactionFilter) for f in handler.filters)
    err = capsys.readouterr().err
    assert "buffered with" in err  # the console still shows records before the folder exists
    assert FICTIONAL_HIDDEN not in err


def test_attach_log_folder_flushes_buffer_in_order_redacted(
    make_buffered_logging: Callable[..., LoggingSetup], log_dir: Path
) -> None:
    import stat

    setup = make_buffered_logging("INFO")
    logger = logging.getLogger("jobpipeline.test.buffer")
    logger.info("first fictional record")
    logger.debug("dropped below the level")
    logger.warning("second with %s", FICTIONAL_HIDDEN)
    try:
        raise RuntimeError(f"fictional failure {FICTIONAL_HIDDEN}")
    except RuntimeError:
        logger.exception("third with a traceback")
    assert not log_dir.exists()

    setup.attach_log_folder(log_dir)
    logger.info("fourth after attaching")

    records = read_log_records(log_dir)
    assert [r["message"] for r in records] == [
        "first fictional record",
        f"second with {REDACTED}",
        "third with a traceback",
        "fourth after attaching",
    ]
    assert "Traceback" in str(records[2]["exception"])
    assert REDACTED in str(records[2]["exception"])
    assert records[0]["logger"] == "jobpipeline.test.buffer"
    assert FICTIONAL_HIDDEN not in read_log_text(log_dir)
    if os.name == "posix":
        assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((log_dir / LOG_FILE_NAME).stat().st_mode) == 0o600


def test_attach_log_folder_leaves_file_and_console_handlers_only(
    make_buffered_logging: Callable[..., LoggingSetup], log_dir: Path
) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    setup = make_buffered_logging("INFO")

    setup.attach_log_folder(log_dir)

    added = [h for h in root.handlers if h not in before]
    assert len(added) == 2, "the buffer is removed once the file handler takes over"
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in added)
    assert any(type(h) is logging.StreamHandler and h.stream is sys.stderr for h in added)

    setup.close()
    assert root.handlers == before


def test_detach_log_folder_closes_the_file_and_allows_a_new_attach(
    make_buffered_logging: Callable[..., LoggingSetup], log_dir: Path, tmp_path: Path
) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    setup = make_buffered_logging("INFO")
    logger = logging.getLogger("jobpipeline.test.buffer")
    setup.attach_log_folder(log_dir)
    logger.info("first record")
    flush_root()

    setup.detach_log_folder()
    setup.detach_log_folder()  # nothing attached: no-op

    assert not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers)
    logger.info("while detached")
    assert "while detached" not in read_log_text(log_dir)
    other = tmp_path / "other-logs"
    setup.attach_log_folder(other)
    logger.info("after reattach")
    assert [r["message"] for r in read_log_records(other)] == ["after reattach"]

    setup.close()
    assert root.handlers == before


def test_detach_log_folder_works_when_the_log_folder_was_attached_from_the_start(
    make_logging: Callable[..., LoggingSetup], log_dir: Path, tmp_path: Path
) -> None:
    """A fixed data folder logs into its file from the start (no buffer): detaching closes it
    and the same folder can be attached again after delete all data."""
    root = logging.getLogger()
    setup = make_logging("INFO")
    logger = logging.getLogger("jobpipeline.test.fixed")
    logger.info("first record")

    setup.detach_log_folder()

    assert not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers)
    logger.info("while detached")
    assert "while detached" not in read_log_text(log_dir)
    other = tmp_path / "other-logs"
    setup.attach_log_folder(other)
    logger.info("after reattach")
    assert [r["message"] for r in read_log_records(other)] == ["after reattach"]


def test_buffer_keeps_most_recent_records_only(
    make_buffered_logging: Callable[..., LoggingSetup], log_dir: Path
) -> None:
    setup = make_buffered_logging("INFO", buffer_limit=3)
    logger = logging.getLogger("jobpipeline.test.buffer")
    for index in range(5):
        logger.info("record %d", index)

    setup.attach_log_folder(log_dir)

    assert [r["message"] for r in read_log_records(log_dir)] == [
        "2 earlier log records were dropped",  # added by the M1.5 review: drops are reported
        "record 2",
        "record 3",
        "record 4",
    ]


def test_attach_log_folder_reports_dropped_records(
    make_buffered_logging: Callable[..., LoggingSetup], log_dir: Path
) -> None:
    setup = make_buffered_logging("INFO", buffer_limit=2)
    logger = logging.getLogger("jobpipeline.test.buffer")
    for index in range(7):
        logger.info("record %d", index)

    setup.attach_log_folder(log_dir)
    logger.info("after attaching")

    records = read_log_records(log_dir)
    assert records[0]["message"] == "5 earlier log records were dropped"
    assert records[0]["level"] == "INFO"
    assert [r["message"] for r in records[1:]] == ["record 5", "record 6", "after attaching"]


def test_attach_log_folder_without_drops_writes_no_note(
    make_buffered_logging: Callable[..., LoggingSetup], log_dir: Path
) -> None:
    setup = make_buffered_logging("INFO", buffer_limit=3)
    logging.getLogger("jobpipeline.test.buffer").info("only record")

    setup.attach_log_folder(log_dir)

    assert [r["message"] for r in read_log_records(log_dir)] == ["only record"]


def test_buffered_record_redacted_with_secret_registered_later(
    make_buffered_logging: Callable[..., LoggingSetup], redactor: Redactor, log_dir: Path
) -> None:
    late = "FictionalLateSecret-0077-notreal"
    setup = make_buffered_logging("INFO")
    logger = logging.getLogger("jobpipeline.test.buffer")
    logger.warning("logged before registration: %s", late, extra={"note": [late]})
    try:
        raise RuntimeError(f"fictional failure {late}")
    except RuntimeError:
        logger.exception("traceback before registration")

    redactor.register(late)
    setup.attach_log_folder(log_dir)

    text = read_log_text(log_dir)
    assert late not in text
    assert f"logged before registration: {REDACTED}" in text


def test_buffer_forwards_records_arriving_after_hand_over(
    make_buffered_logging: Callable[..., LoggingSetup], log_dir: Path
) -> None:
    """A thread that reached the buffer while it was being replaced loses nothing."""
    from app.logging_config import _BufferHandler

    setup = make_buffered_logging("INFO")
    buffer = next(h for h in logging.getLogger().handlers if isinstance(h, _BufferHandler))
    setup.attach_log_folder(log_dir)

    buffer.handle(logging.LogRecord("jobpipeline.late", logging.INFO, "", 0, "late", None, None))

    assert [r["message"] for r in read_log_records(log_dir)] == ["late"]


def test_attach_log_folder_twice_raises(
    make_buffered_logging: Callable[..., LoggingSetup], log_dir: Path, tmp_path: Path
) -> None:
    setup = make_buffered_logging("INFO")
    setup.attach_log_folder(log_dir)
    other = tmp_path / "other" / "logs"

    with pytest.raises(RuntimeError):
        setup.attach_log_folder(other)

    assert not other.exists()


def test_attach_log_folder_when_configured_with_folder_raises(
    make_logging: Callable[..., LoggingSetup], tmp_path: Path
) -> None:
    setup = make_logging("INFO")
    other = tmp_path / "other" / "logs"

    with pytest.raises(RuntimeError):
        setup.attach_log_folder(other)

    assert not other.exists()


def test_close_without_folder_removes_buffer_and_console(
    make_buffered_logging: Callable[..., LoggingSetup], tmp_path: Path
) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    setup = make_buffered_logging("INFO")
    logging.getLogger("jobpipeline.test.buffer").info("never written to a file")

    setup.close()

    assert root.handlers == before
    assert list(tmp_path.iterdir()) == []


def test_late_record_after_close_does_not_reopen_the_log_file(tmp_path: Path) -> None:
    """A thread stuck at the old buffer until shutdown must not reopen the closed file."""
    setup = configure_logging(None, "INFO", Redactor())
    buffer = next(h for h in logging.getLogger().handlers if type(h).__name__ == "_BufferHandler")
    log_dir = tmp_path / "logs"
    setup.attach_log_folder(log_dir)
    setup.close()
    size_after_close = sum(f.stat().st_size for f in log_dir.iterdir())

    buffer.handle(logging.makeLogRecord({"msg": "late record", "levelno": logging.INFO}))

    assert sum(f.stat().st_size for f in log_dir.iterdir()) == size_after_close


# --- Claude Agent SDK records (M1.13 review; spec 3.3) ----------------------------------------

SDK_LINE_TEXT = "Fictional prompt text from a CLI stdout line"


@pytest.mark.parametrize(
    "name", ["claude_agent_sdk", "claude_agent_sdk._internal.query", "claude_agent_sdk.x.y"]
)
def test_sdk_record_filter_keeps_only_level_logger_and_exception_type(name: str) -> None:
    try:
        raise ValueError(SDK_LINE_TEXT)
    except ValueError:
        record = logging.LogRecord(
            name, logging.ERROR, __file__, 1, "Fatal error: %s", (SDK_LINE_TEXT,), sys.exc_info()
        )
    record.stack_info = SDK_LINE_TEXT
    record.line = SDK_LINE_TEXT  # an extra

    assert SdkRecordFilter().filter(record) is True

    assert record.getMessage() == f"ERROR from {name} (ValueError)"
    assert record.args is None
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None
    assert not hasattr(record, "line")
    assert SDK_LINE_TEXT not in JsonFormatter().format(record)


def test_sdk_record_filter_without_exception_names_level_and_logger() -> None:
    record = logging.LogRecord(
        "claude_agent_sdk._internal.transport.subprocess_cli",
        logging.DEBUG,
        __file__,
        1,
        "Skipping non-JSON line from CLI stdout: %s",
        (SDK_LINE_TEXT,),
        None,
    )

    SdkRecordFilter().filter(record)

    assert record.getMessage() == ("DEBUG from claude_agent_sdk._internal.transport.subprocess_cli")


@pytest.mark.parametrize("name", ["jobpipeline.test", "claude_agent_sdk_other", "claude"])
def test_sdk_record_filter_leaves_other_loggers_alone(name: str) -> None:
    record = logging.LogRecord(name, logging.INFO, __file__, 1, "kept %s", ("text",), None)

    SdkRecordFilter().filter(record)

    assert record.getMessage() == "kept text"


def test_sdk_records_reach_handlers_reduced(
    make_logging: Callable[..., LoggingSetup],
    log_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = list(logging.getLogger().handlers)
    make_logging("DEBUG")
    added = [h for h in logging.getLogger().handlers if h not in before]
    assert len(added) == 2
    assert all(any(isinstance(f, SdkRecordFilter) for f in h.filters) for h in added)

    logging.getLogger("claude_agent_sdk._internal.query").error(
        "Fatal error in message reader: %s", SDK_LINE_TEXT
    )

    records = read_log_records(log_dir)
    err = capsys.readouterr().err
    assert records[-1]["message"] == "ERROR from claude_agent_sdk._internal.query"
    assert SDK_LINE_TEXT not in read_log_text(log_dir)
    assert SDK_LINE_TEXT not in err


def test_buffered_sdk_records_are_reduced(
    tmp_path: Path, redactor: Redactor, restore_logging: None
) -> None:
    setup = configure_logging(None, "DEBUG", redactor)
    try:
        logging.getLogger("claude_agent_sdk").warning("line: %s", SDK_LINE_TEXT)
        log_dir = tmp_path / "later" / "logs"
        setup.attach_log_folder(log_dir)
        assert SDK_LINE_TEXT not in read_log_text(log_dir)
    finally:
        setup.close()


# --- SDK records reduced at creation (M1.23; AC-M1.4) ------------------------------------------

SDK_CHILD = "claude_agent_sdk._internal.query"


@pytest.fixture
def plain_record_factory() -> Iterator[Callable[..., logging.LogRecord]]:
    """The standard record factory for this test (the session installs the SDK reduction);
    whatever factory was in place comes back afterwards."""
    saved = logging.getLogRecordFactory()
    logging.setLogRecordFactory(logging.LogRecord)
    yield logging.LogRecord
    logging.setLogRecordFactory(saved)


class Collect(logging.Handler):
    """A plain handler the app didn't install: no filters, keeps each record as it arrives."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.texts: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.texts.append(logging.Formatter("%(message)s %(name)s").format(record))


@contextmanager
def collecting(name: str) -> Iterator[Collect]:
    logger = logging.getLogger(name)
    handler = Collect()
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def log_sdk_failure(logger: logging.Logger, secret: str) -> None:
    """What the SDK's message reader does: exception text formatted into the message, plus
    args, the exception itself and a stack, all quoting ``secret``."""
    try:
        raise ValueError(f"CLI stderr: invalid token {secret}")
    except ValueError as error:
        logger.error(f"Fatal error in message reader: {error}", exc_info=True, stack_info=True)
        logger.warning("stderr line: %s", secret)


def test_sdk_child_record_reduced_at_creation_for_any_handler(
    plain_record_factory: Callable[..., logging.LogRecord],
) -> None:
    secret = anthropic_shaped("oat01")
    restore = install_sdk_record_reduction()
    try:
        with collecting(SDK_CHILD) as handler:
            log_sdk_failure(logging.getLogger(SDK_CHILD), secret)
    finally:
        restore()

    assert [r.getMessage() for r in handler.records] == [
        f"ERROR from {SDK_CHILD} (ValueError)",
        f"WARNING from {SDK_CHILD}",
    ]
    for record in handler.records:
        assert record.args is None
        assert record.exc_info is None
        assert record.exc_text is None
        assert record.stack_info is None
    assert handler.texts and not any(secret in text for text in handler.texts)


def test_sdk_record_reduced_before_last_resort_handler(
    plain_record_factory: Callable[..., logging.LogRecord],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no handler anywhere (e.g. after ``LoggingSetup.close``), ``logging.lastResort``
    writes the record to stderr: without the reduction the key is there, with it it isn't."""
    secret = anthropic_shaped("api03")
    logger = logging.getLogger("claude_agent_sdk.m123_no_handlers")
    logger.propagate = False
    try:
        assert not logger.handlers
        log_sdk_failure(logger, secret)
        assert secret in capsys.readouterr().err, "control: lastResort writes the raw record"

        restore = install_sdk_record_reduction()
        try:
            log_sdk_failure(logger, secret)
        finally:
            restore()
    finally:
        logger.propagate = True

    err = capsys.readouterr().err
    assert secret not in err
    assert "ERROR from claude_agent_sdk.m123_no_handlers (ValueError)" in err


def test_session_reduces_sdk_records_seen_by_caplog(caplog: pytest.LogCaptureFixture) -> None:
    """The session-wide fixture in ``tests/conftest.py`` covers pytest's own log capture."""
    secret = anthropic_shaped("oat01")
    with caplog.at_level(logging.DEBUG):
        log_sdk_failure(logging.getLogger(SDK_CHILD), secret)

    sdk = [r for r in caplog.records if r.name == SDK_CHILD]
    assert [r.getMessage() for r in sdk] == [
        f"ERROR from {SDK_CHILD} (ValueError)",
        f"WARNING from {SDK_CHILD}",
    ]
    assert secret not in caplog.text


@pytest.mark.parametrize("name", ["jobpipeline.test", "claude_agent_sdk_other", "claude"])
def test_sdk_record_reduction_leaves_other_records_unchanged(
    name: str, plain_record_factory: Callable[..., logging.LogRecord]
) -> None:
    restore = install_sdk_record_reduction()
    try:
        with collecting(name) as handler:
            try:
                raise ValueError("kept detail")
            except ValueError:
                logging.getLogger(name).error("kept %s", "text", exc_info=True)
    finally:
        restore()

    (record,) = handler.records
    assert record.getMessage() == "kept text"
    assert record.args == ("text",)
    assert record.exc_info is not None and str(record.exc_info[1]) == "kept detail"


def test_sdk_record_reduced_at_creation_keeps_its_summary_through_handler_filters(
    plain_record_factory: Callable[..., logging.LogRecord],
) -> None:
    """Each app handler's ``SdkRecordFilter`` sees the record again (and so may pytest's
    caplog handler); the exception type named at creation stays."""
    secret = anthropic_shaped("oat01")
    restore = install_sdk_record_reduction()
    try:
        try:
            raise ValueError(secret)
        except ValueError:
            record = logging.getLogger(SDK_CHILD).makeRecord(
                SDK_CHILD, logging.ERROR, __file__, 1, "error: %s", (secret,), sys.exc_info()
            )
    finally:
        restore()

    for _ in range(2):
        assert SdkRecordFilter().filter(record) is True
        assert record.getMessage() == f"ERROR from {SDK_CHILD} (ValueError)"
    formatted = JsonFormatter().format(record)
    assert secret not in formatted
    assert "sdk_reduced" not in formatted, "the internal marker is not written"


def test_sdk_record_filter_rebuilds_a_record_that_arrives_marked() -> None:
    """A record that wasn't made by the factory but carries the marker can't keep its text,
    and a marker value that isn't an exception type name can't be shown either."""
    secret = anthropic_shaped("api03")
    for marker in (True, secret, "ValueError"):
        record = logging.makeLogRecord(
            {"name": SDK_CHILD, "msg": f"leak {secret}", "args": None, SDK_REDUCED_ATTR: marker}
        )
        record.levelname = "ERROR"

        SdkRecordFilter().filter(record)

        suffix = " (ValueError)" if marker == "ValueError" else ""
        assert record.getMessage() == f"ERROR from {SDK_CHILD}{suffix}"
        assert secret not in JsonFormatter().format(record)


def test_sdk_record_reduction_restore_puts_previous_factory_back(
    plain_record_factory: Callable[..., logging.LogRecord],
) -> None:
    restore = install_sdk_record_reduction()
    assert logging.getLogRecordFactory() is not plain_record_factory

    restore()
    assert logging.getLogRecordFactory() is plain_record_factory
    restore()  # a second call changes nothing
    assert logging.getLogRecordFactory() is plain_record_factory


def test_sdk_record_reduction_restore_keeps_a_factory_installed_later(
    plain_record_factory: Callable[..., logging.LogRecord],
) -> None:
    restore = install_sdk_record_reduction()
    ours = logging.getLogRecordFactory()

    def later(*args: object, **kwargs: object) -> logging.LogRecord:
        return ours(*args, **kwargs)

    logging.setLogRecordFactory(later)
    restore()

    assert logging.getLogRecordFactory() is later
    record = later(SDK_CHILD, logging.ERROR, __file__, 1, "text %s", ("detail",), None)
    assert record.getMessage() == f"ERROR from {SDK_CHILD}", "still reduced inside the chain"


def test_sdk_record_reduction_double_install_is_safe(
    plain_record_factory: Callable[..., logging.LogRecord],
) -> None:
    first = install_sdk_record_reduction()
    installed = logging.getLogRecordFactory()
    second = install_sdk_record_reduction()
    assert logging.getLogRecordFactory() is installed, "not wrapped twice"

    second()
    assert logging.getLogRecordFactory() is installed, "the inner install's restore is a no-op"
    record = installed(SDK_CHILD, logging.INFO, __file__, 1, "text %s", ("detail",), None)
    assert record.getMessage() == f"INFO from {SDK_CHILD}"

    first()
    assert logging.getLogRecordFactory() is plain_record_factory


def test_configure_logging_installs_sdk_reduction_and_close_restores_it(
    plain_record_factory: Callable[..., logging.LogRecord],
    redactor: Redactor,
    restore_logging: None,
) -> None:
    setup = configure_logging(None, "DEBUG", redactor)
    try:
        factory = logging.getLogRecordFactory()
        record = factory(SDK_CHILD, logging.ERROR, __file__, 1, "text %s", ("detail",), None)
        assert record.getMessage() == f"ERROR from {SDK_CHILD}"
    finally:
        setup.close()
    assert logging.getLogRecordFactory() is plain_record_factory
    setup.close()  # closing twice keeps it restored
    assert logging.getLogRecordFactory() is plain_record_factory


# --- exception text and HTTP client records (M4.11 review; spec 2.10, TD-228) -------------------

ERROR_MARKER = "FictionalErrorMarkerCormorant-5512"
URL_MARKER = "FictionalLocationMarkerPlover-8840"


def _log_surfaces(log_dir: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """Everything the log file, stdout and stderr hold, as one text."""
    captured = capsys.readouterr()
    return read_log_text(log_dir) + captured.out + captured.err


def _raised_runtime_error() -> tuple[type[BaseException], BaseException, object]:
    try:
        raise RuntimeError(f"fictional failure {ERROR_MARKER}")
    except RuntimeError:
        exc_type, exc, tb = sys.exc_info()
    assert exc_type is not None and exc is not None
    return exc_type, exc, tb


def test_uvicorn_exception_record_keeps_level_logger_and_exception_type_only(
    make_logging: Callable[..., LoggingSetup],
    log_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_logging("DEBUG")

    # Exactly how uvicorn reports an unexpected error in a route.
    logging.getLogger("uvicorn.error").error(
        "Exception in ASGI application\n", exc_info=_raised_runtime_error()
    )

    seen = _log_surfaces(log_dir, capsys)
    assert ERROR_MARKER not in seen
    assert "Traceback" not in seen
    record = read_log_records(log_dir)[-1]
    assert record["level"] == "ERROR"
    assert record["logger"] == "uvicorn.error"
    assert record["message"] == "ERROR from uvicorn.error (RuntimeError)"
    assert "exception" not in record


def test_uvicorn_records_without_an_exception_are_unchanged(
    make_logging: Callable[..., LoggingSetup], log_dir: Path
) -> None:
    make_logging("DEBUG")

    logging.getLogger("uvicorn.error").info("Application startup complete.")

    assert read_log_records(log_dir)[-1]["message"] == "Application startup complete."


def test_uncaught_exception_hooks_log_the_exception_type_only(
    make_logging: Callable[..., LoggingSetup],
    log_dir: Path,
    capsys: pytest.CaptureFixture[str],
    restore_hooks: None,
) -> None:
    from app.logging_config import install_exception_hooks

    make_logging("DEBUG")
    restore = install_exception_hooks("jobpipeline.test.hooks")
    try:
        exc_type, exc, tb = _raised_runtime_error()
        sys.excepthook(exc_type, exc, tb)  # type: ignore[arg-type]

        def fail() -> None:
            raise ValueError(f"fictional thread failure {ERROR_MARKER}")

        worker = threading.Thread(target=fail, name="fictional-worker")
        worker.start()
        worker.join(5.0)
    finally:
        restore()

    assert ERROR_MARKER not in _log_surfaces(log_dir, capsys)
    messages = [str(r["message"]) for r in read_log_records(log_dir)]
    assert "CRITICAL from jobpipeline.test.hooks (RuntimeError)" in messages
    assert "CRITICAL from jobpipeline.test.hooks (ValueError)" in messages


def test_start_up_failure_traceback_is_still_kept_in_the_file(
    make_logging: Callable[..., LoggingSetup], log_dir: Path
) -> None:
    """Only uvicorn and the uncaught-exception hooks are reduced: the CLI's start-up failure
    (no job text exists yet) keeps its traceback for the developer."""
    make_logging("DEBUG")

    logging.getLogger("jobpipeline.cli").critical(
        "Start-up failed", exc_info=_raised_runtime_error()
    )

    assert "fictional failure" in str(read_log_records(log_dir)[-1]["exception"])


@pytest.mark.integration
def test_a_route_error_under_real_uvicorn_never_logs_its_message(
    make_logging: Callable[..., LoggingSetup],
    log_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A route raising an unexpected RuntimeError whose text holds a marker, served by uvicorn
    with the CLI's own config: the marker reaches no log file, stdout or stderr, the exception
    type does."""
    import time

    import httpx
    import uvicorn
    from fastapi import FastAPI

    from app.cli import server_config

    app = FastAPI()

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError(f"fictional job text {ERROR_MARKER}")

    make_logging("DEBUG")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(server_config(app, port))
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline and thread.is_alive():
            time.sleep(0.02)
        assert server.started
        response = httpx.get(f"http://127.0.0.1:{port}/boom", timeout=5)
        assert response.status_code == 500
    finally:
        server.should_exit = True
        thread.join(10)

    seen = _log_surfaces(log_dir, capsys)
    assert ERROR_MARKER not in seen
    messages = [str(r["message"]) for r in read_log_records(log_dir)]
    assert "ERROR from uvicorn.error (RuntimeError)" in messages


@pytest.mark.parametrize(
    "name",
    ["httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpx2", "httpcore2.http11"],
)
def test_http_client_records_below_warning_never_reach_a_handler(
    name: str,
    make_logging: Callable[..., LoggingSetup],
    log_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_logging("DEBUG")
    logger = logging.getLogger(name)
    previous = logger.level
    logger.setLevel(logging.NOTSET)  # as if nothing had silenced it
    try:
        logger.debug("connect_tcp.started host=%r", f"{URL_MARKER}.example.test")
        logger.info('HTTP Request: GET https://x.example.test/%s "HTTP/1.1 302"', URL_MARKER)
        logger.debug("response headers: Location: https://x.example.test/%s", URL_MARKER)
    finally:
        logger.setLevel(previous)

    seen = _log_surfaces(log_dir, capsys)
    assert URL_MARKER not in seen
    assert f"{name}" not in "".join(str(r["logger"]) for r in read_log_records(log_dir))


def test_http_client_records_from_warning_up_keep_level_logger_and_exception_type(
    make_logging: Callable[..., LoggingSetup],
    log_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_logging("DEBUG")
    try:
        raise ConnectionError(f"cannot connect to https://x.example.test/{URL_MARKER}")
    except ConnectionError:
        exc_info = sys.exc_info()

    logging.getLogger("httpcore.connection").warning(
        "retrying https://x.example.test/%s", URL_MARKER, exc_info=exc_info
    )
    logging.getLogger("httpx").error("failed https://x.example.test/%s", URL_MARKER)

    assert URL_MARKER not in _log_surfaces(log_dir, capsys)
    messages = {str(r["logger"]): str(r["message"]) for r in read_log_records(log_dir)}
    assert messages["httpcore.connection"] == "WARNING from httpcore.connection (ConnectionError)"
    assert messages["httpx"] == "ERROR from httpx"
