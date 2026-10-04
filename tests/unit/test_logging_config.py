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
import sys
import threading
from collections.abc import Callable, Iterator
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
    JsonFormatter,
    LoggingSetup,
    RedactionFilter,
    configure_logging,
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
    assert f"/setup/callback?token={REDACTED}" in str(access_records[0]["message"])


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
