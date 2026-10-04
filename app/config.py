"""Runtime config and data folder resolution (spec 2.8 and 7.3; ADR-024, 025, 030).

Precedence is CLI flag, then environment variable, then bootstrap config, then default. The
environment is passed in, never read from ``os.environ``, so resolution is easy to test.
"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

from app.domain.errors import ConfigError

APP_NAME = "jobpipeline"
DEMO_APP_NAME = "jobpipeline-demo"
BOOTSTRAP_FILE = "config.toml"
DEFAULT_PORT = 8000
DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

ENV_DATA_DIR = "JOBPIPELINE_DATA_DIR"
ENV_CONFIG_DIR = "JOBPIPELINE_CONFIG_DIR"
ENV_PORT = "JOBPIPELINE_PORT"
ENV_LOG_LEVEL = "JOBPIPELINE_LOG_LEVEL"


@dataclass(frozen=True)
class BootstrapConfig:
    data_dir: Path
    version: str


@dataclass(frozen=True)
class CliOptions:
    data_dir: Path | None = None
    port: int | None = None
    log_level: str | None = None
    demo: bool = False
    setup: bool = False
    no_browser: bool = False


@dataclass(frozen=True)
class RuntimeConfig:
    data_dir: Path
    config_dir: Path
    port: int
    log_level: str
    demo: bool
    setup: bool
    no_browser: bool


def resolve_config_dir(env: Mapping[str, str]) -> Path:
    override = env.get(ENV_CONFIG_DIR)
    if override is None:
        return Path(user_config_dir(APP_NAME))
    return _env_path(ENV_CONFIG_DIR, override)


def load_bootstrap(config_dir: Path) -> BootstrapConfig | None:
    """Read ``config.toml``; ``None`` when it is absent (first run)."""
    path = config_dir / BOOTSTRAP_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(detail=f"settings file can't be read ({type(error).__name__})") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ConfigError(detail="settings file is not valid TOML") from None
    data_dir, version = data.get("data_dir"), data.get("version")
    if not isinstance(data_dir, str) or not data_dir:
        raise ConfigError(detail="settings file needs a data_dir text value")
    if not isinstance(version, str):
        raise ConfigError(detail="settings file needs a version text value")
    data_path = Path(data_dir).expanduser()
    if not data_path.is_absolute():
        raise ConfigError(detail="settings file data_dir must be an absolute path")
    return BootstrapConfig(data_dir=data_path, version=version)


def resolve_config(cli: CliOptions, env: Mapping[str, str]) -> RuntimeConfig:
    config_dir = resolve_config_dir(env)
    bootstrap = load_bootstrap(config_dir)
    return RuntimeConfig(
        data_dir=_resolve_data_dir(cli, env, bootstrap),
        config_dir=config_dir,
        port=_resolve_port(cli, env),
        log_level=_resolve_log_level(cli, env),
        demo=cli.demo,
        setup=cli.setup,
        no_browser=cli.no_browser,
    )


def _resolve_data_dir(
    cli: CliOptions, env: Mapping[str, str], bootstrap: BootstrapConfig | None
) -> Path:
    if cli.data_dir is not None:
        return cli.data_dir
    if ENV_DATA_DIR in env:
        return _env_path(ENV_DATA_DIR, env[ENV_DATA_DIR])
    if cli.demo:
        return Path(user_data_dir(DEMO_APP_NAME))
    if bootstrap is not None:
        return bootstrap.data_dir
    return Path(user_data_dir(APP_NAME))


def _resolve_port(cli: CliOptions, env: Mapping[str, str]) -> int:
    if cli.port is not None:
        return _check_port(cli.port, "--port")
    if ENV_PORT in env:
        raw = env[ENV_PORT]
        if not (raw.isascii() and raw.isdigit()):
            raise ConfigError(detail=f"{ENV_PORT} must be a whole number")
        return _check_port(int(raw), ENV_PORT)
    return DEFAULT_PORT


def _check_port(port: int, source: str) -> int:
    if not 1 <= port <= 65535:
        raise ConfigError(detail=f"{source} must be between 1 and 65535")
    return port


def _resolve_log_level(cli: CliOptions, env: Mapping[str, str]) -> str:
    if cli.log_level is not None:
        return _check_log_level(cli.log_level, "--log-level")
    if ENV_LOG_LEVEL in env:
        return _check_log_level(env[ENV_LOG_LEVEL], ENV_LOG_LEVEL)
    return DEFAULT_LOG_LEVEL


def _check_log_level(raw: str, source: str) -> str:
    level = raw.upper()
    if level not in LOG_LEVELS:
        raise ConfigError(detail=f"{source} must be one of {', '.join(LOG_LEVELS)}")
    return level


def _env_path(name: str, raw: str) -> Path:
    if not raw:
        raise ConfigError(detail=f"{name} is empty")
    return Path(raw)
