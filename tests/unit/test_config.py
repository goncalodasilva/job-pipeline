"""Runtime config and data folder resolution (M0.9; spec 2.8 and 7.3; ADR-024, 025, 030).

Precedence is CLI > environment > bootstrap config > defaults. The environment is passed in as
a mapping, so no test reads or changes ``os.environ``. The platform folders are replaced with
temporary ones, so no test touches the developer's real data or config folders.
"""

import dataclasses
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from app.config import (
    BootstrapConfig,
    CliOptions,
    RuntimeConfig,
    load_bootstrap,
    resolve_config,
    resolve_config_dir,
)
from app.domain.errors import AppError, ConfigError

FICTIONAL_VERSION = "0.0.1-test"
MARKER = "FICTIONAL-MARKER-DO-NOT-LEAK"


# --- helpers -------------------------------------------------------------------------------


@pytest.fixture
def platform_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the platformdirs defaults at a temporary folder; return its root."""
    root = tmp_path / "platform"

    def fake_data_dir(appname: str | None = None, *args: object, **kwargs: object) -> str:
        return str(root / "data" / (appname or ""))

    def fake_config_dir(appname: str | None = None, *args: object, **kwargs: object) -> str:
        return str(root / "config" / (appname or ""))

    monkeypatch.setattr("app.config.user_data_dir", fake_data_dir)
    monkeypatch.setattr("app.config.user_config_dir", fake_config_dir)
    return root


def default_data_dir(root: Path) -> Path:
    return root / "data" / "jobpipeline"


def demo_data_dir(root: Path) -> Path:
    return root / "data" / "jobpipeline-demo"


def default_config_dir(root: Path) -> Path:
    return root / "config" / "jobpipeline"


def write_bootstrap(config_dir: Path, *, data_dir: Path, version: str = FICTIONAL_VERSION) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(
        f'data_dir = "{data_dir.as_posix()}"\nversion = "{version}"\n', encoding="utf-8"
    )


def write_raw(config_dir: Path, text: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(text, encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path / "cfg"


@pytest.fixture
def make_env(config_dir: Path) -> Callable[..., dict[str, str]]:
    """Build an environment that always points the config folder at the temporary one."""

    def build(**extra: str) -> dict[str, str]:
        return {"JOBPIPELINE_CONFIG_DIR": str(config_dir), **extra}

    return build


# --- precedence table ----------------------------------------------------------------------

# For each setting: the value each layer supplies, in the form that layer takes it, and the
# value the resolved config must hold. Port and log level have no bootstrap layer (the
# bootstrap file holds only data_dir and version), so that layer is None for them.


@dataclasses.dataclass(frozen=True)
class Layers:
    field: str
    cli: object
    env_name: str
    env: str
    bootstrap: object | None
    default: object
    resolved_cli: object
    resolved_env: object
    resolved_bootstrap: object | None


def layers_for(name: str, root: Path, tmp_path: Path) -> Layers:
    if name == "data_dir":
        cli, env, boot = tmp_path / "from-cli", tmp_path / "from-env", tmp_path / "from-boot"
        return Layers(
            field="data_dir",
            cli=cli,
            env_name="JOBPIPELINE_DATA_DIR",
            env=str(env),
            bootstrap=boot,
            default=default_data_dir(root),
            resolved_cli=cli,
            resolved_env=env,
            resolved_bootstrap=boot,
        )
    if name == "port":
        return Layers(
            field="port",
            cli=9101,
            env_name="JOBPIPELINE_PORT",
            env="9102",
            bootstrap=None,
            default=8000,
            resolved_cli=9101,
            resolved_env=9102,
            resolved_bootstrap=None,
        )
    return Layers(
        field="log_level",
        cli="debug",
        env_name="JOBPIPELINE_LOG_LEVEL",
        env="warning",
        bootstrap=None,
        default="INFO",
        resolved_cli="DEBUG",
        resolved_env="WARNING",
        resolved_bootstrap=None,
    )


@pytest.mark.parametrize("name", ["data_dir", "port", "log_level"])
def test_precedence_cli_env_bootstrap_default(
    name: str,
    platform_dirs: Path,
    tmp_path: Path,
    config_dir: Path,
    make_env: Callable[..., dict[str, str]],
) -> None:
    layers = layers_for(name, platform_dirs, tmp_path)
    # The bootstrap file always exists with a data_dir; it only matters for data_dir.
    boot_dir = layers.bootstrap if isinstance(layers.bootstrap, Path) else tmp_path / "unused"
    write_bootstrap(config_dir, data_dir=boot_dir)
    cli_with = CliOptions(**{layers.field: layers.cli})

    # 1. All four layers present: the CLI wins.
    resolved = resolve_config(cli_with, make_env(**{layers.env_name: layers.env}))
    assert getattr(resolved, layers.field) == layers.resolved_cli

    # 2. No CLI value: the environment wins over the bootstrap file and the default.
    resolved = resolve_config(CliOptions(), make_env(**{layers.env_name: layers.env}))
    assert getattr(resolved, layers.field) == layers.resolved_env

    # 3. No CLI or environment value: the bootstrap file wins where it holds the setting,
    #    otherwise the default applies.
    resolved = resolve_config(CliOptions(), make_env())
    expected = layers.resolved_bootstrap if layers.bootstrap is not None else layers.default
    assert getattr(resolved, layers.field) == expected

    # 4. No bootstrap file at all: the default.
    (config_dir / "config.toml").unlink()
    resolved = resolve_config(CliOptions(), make_env())
    assert getattr(resolved, layers.field) == layers.default


def test_defaults_when_nothing_is_set(
    platform_dirs: Path, make_env: Callable[..., dict[str, str]], config_dir: Path
) -> None:
    resolved = resolve_config(CliOptions(), make_env())

    assert resolved == RuntimeConfig(
        data_dir=default_data_dir(platform_dirs),
        config_dir=config_dir,
        port=8000,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
    )


def test_cli_flags_are_carried_into_the_runtime_config(
    platform_dirs: Path, make_env: Callable[..., dict[str, str]]
) -> None:
    resolved = resolve_config(CliOptions(demo=True, setup=True, no_browser=True), make_env())

    assert (resolved.demo, resolved.setup, resolved.no_browser) == (True, True, True)


def test_env_value_wins_over_bootstrap_for_data_dir_even_when_both_exist(
    platform_dirs: Path,
    tmp_path: Path,
    config_dir: Path,
    make_env: Callable[..., dict[str, str]],
) -> None:
    write_bootstrap(config_dir, data_dir=tmp_path / "from-boot")

    resolved = resolve_config(
        CliOptions(), make_env(JOBPIPELINE_DATA_DIR=str(tmp_path / "from-env"))
    )

    assert resolved.data_dir == tmp_path / "from-env"


# --- demo mode (ADR-025) -------------------------------------------------------------------


def test_demo_uses_its_own_folder_and_ignores_bootstrap_data_dir(
    platform_dirs: Path,
    tmp_path: Path,
    config_dir: Path,
    make_env: Callable[..., dict[str, str]],
) -> None:
    write_bootstrap(config_dir, data_dir=tmp_path / "real-data")

    resolved = resolve_config(CliOptions(demo=True), make_env())

    assert resolved.demo is True
    assert resolved.data_dir == demo_data_dir(platform_dirs)
    assert resolved.data_dir != tmp_path / "real-data"


def test_demo_folder_is_a_sibling_of_the_default_data_folder(
    platform_dirs: Path, make_env: Callable[..., dict[str, str]]
) -> None:
    demo = resolve_config(CliOptions(demo=True), make_env()).data_dir
    real = resolve_config(CliOptions(), make_env()).data_dir

    assert demo != real
    assert demo.parent == real.parent


def test_data_dir_wins_over_demo(
    platform_dirs: Path,
    tmp_path: Path,
    config_dir: Path,
    make_env: Callable[..., dict[str, str]],
) -> None:
    write_bootstrap(config_dir, data_dir=tmp_path / "real-data")

    resolved = resolve_config(CliOptions(demo=True, data_dir=tmp_path / "chosen"), make_env())

    assert resolved.demo is True
    assert resolved.data_dir == tmp_path / "chosen"


def test_demo_with_env_data_dir_uses_the_env_folder(
    platform_dirs: Path, tmp_path: Path, make_env: Callable[..., dict[str, str]]
) -> None:
    # Only the bootstrap data_dir is ignored in demo mode (ADR-025). An explicit environment
    # override is a deliberate choice for this run, so it beats the demo folder; --data-dir
    # beats both.
    env = make_env(JOBPIPELINE_DATA_DIR=str(tmp_path / "from-env"))

    from_env = resolve_config(CliOptions(demo=True), env)
    from_cli = resolve_config(CliOptions(demo=True, data_dir=tmp_path / "from-cli"), env)

    assert from_env.data_dir == tmp_path / "from-env"
    assert from_cli.data_dir == tmp_path / "from-cli"


# --- invalid values ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["abc", "", "80.5", "0", "-1", "65536", "99999", "8_0", " 80", "+80"]
)
def test_invalid_port_env_raises_config_error(
    value: str, platform_dirs: Path, make_env: Callable[..., dict[str, str]]
) -> None:
    with pytest.raises(ConfigError):
        resolve_config(CliOptions(), make_env(JOBPIPELINE_PORT=value))


@pytest.mark.parametrize("value", [0, -5, 65536])
def test_out_of_range_cli_port_raises_config_error(
    value: int, platform_dirs: Path, make_env: Callable[..., dict[str, str]]
) -> None:
    with pytest.raises(ConfigError):
        resolve_config(CliOptions(port=value), make_env())


@pytest.mark.parametrize("value", [1, 8000, 65535])
def test_port_bounds_are_accepted(
    value: int, platform_dirs: Path, make_env: Callable[..., dict[str, str]]
) -> None:
    assert resolve_config(CliOptions(port=value), make_env()).port == value
    assert resolve_config(CliOptions(), make_env(JOBPIPELINE_PORT=str(value))).port == value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("debug", "DEBUG"),
        ("Info", "INFO"),
        ("WARNING", "WARNING"),
        ("error", "ERROR"),
    ],
)
def test_log_level_is_case_insensitive_and_normalised(
    raw: str, expected: str, platform_dirs: Path, make_env: Callable[..., dict[str, str]]
) -> None:
    from_env = resolve_config(CliOptions(), make_env(JOBPIPELINE_LOG_LEVEL=raw))
    from_cli = resolve_config(CliOptions(log_level=raw), make_env())

    assert from_env.log_level == expected
    assert from_cli.log_level == expected


@pytest.mark.parametrize("raw", ["LOUD", "", "trace", "10"])
def test_unknown_log_level_raises_config_error(
    raw: str, platform_dirs: Path, make_env: Callable[..., dict[str, str]]
) -> None:
    with pytest.raises(ConfigError):
        resolve_config(CliOptions(), make_env(JOBPIPELINE_LOG_LEVEL=raw))
    with pytest.raises(ConfigError):
        resolve_config(CliOptions(log_level=raw), make_env())


# --- bootstrap config ----------------------------------------------------------------------


def test_missing_bootstrap_is_none(config_dir: Path) -> None:
    assert load_bootstrap(config_dir) is None  # folder does not exist

    config_dir.mkdir()
    assert load_bootstrap(config_dir) is None  # folder exists, file does not


def test_valid_bootstrap_is_loaded(config_dir: Path, tmp_path: Path) -> None:
    write_bootstrap(config_dir, data_dir=tmp_path / "my-data", version="1.2.3")

    loaded = load_bootstrap(config_dir)

    assert loaded == BootstrapConfig(data_dir=tmp_path / "my-data", version="1.2.3")
    assert isinstance(loaded.data_dir, Path)


def test_missing_bootstrap_means_first_run_and_resolves_defaults(
    platform_dirs: Path, make_env: Callable[..., dict[str, str]]
) -> None:
    assert resolve_config(CliOptions(), make_env()).data_dir == default_data_dir(platform_dirs)


MALFORMED = {
    "not_toml": f"this is = = not toml {MARKER}\n[",
    "empty_file": "",
    "missing_data_dir": f'version = "{FICTIONAL_VERSION}"\n',
    "missing_version": 'data_dir = "/somewhere"\n',
    "data_dir_wrong_type": f'data_dir = 42\nversion = "{FICTIONAL_VERSION}"\n',
    "version_wrong_type": 'data_dir = "/somewhere"\nversion = 3\n',
    "data_dir_empty": f'data_dir = ""\nversion = "{FICTIONAL_VERSION}"\n',
    "data_dir_relative": f'data_dir = "data"\nversion = "{FICTIONAL_VERSION}"\n',
    "binary_garbage": "\x00\x01\x02 " + MARKER,
}


@pytest.mark.parametrize("text", list(MALFORMED.values()), ids=list(MALFORMED))
def test_malformed_bootstrap_raises_config_error(text: str, config_dir: Path) -> None:
    write_raw(config_dir, text)

    with pytest.raises(ConfigError):
        load_bootstrap(config_dir)


@pytest.mark.parametrize("text", list(MALFORMED.values()), ids=list(MALFORMED))
def test_malformed_bootstrap_stops_resolution_with_the_same_error(
    text: str,
    platform_dirs: Path,
    config_dir: Path,
    make_env: Callable[..., dict[str, str]],
) -> None:
    write_raw(config_dir, text)

    with pytest.raises(ConfigError):
        resolve_config(CliOptions(), make_env())


def test_bootstrap_data_dir_with_tilde_is_expanded(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    write_raw(config_dir, f'data_dir = "~/jp-data"\nversion = "{FICTIONAL_VERSION}"\n')

    loaded = load_bootstrap(config_dir)

    assert loaded is not None
    assert loaded.data_dir == tmp_path / "home" / "jp-data"


def test_malformed_bootstrap_still_fails_with_demo_or_cli_data_dir(
    platform_dirs: Path,
    tmp_path: Path,
    config_dir: Path,
    make_env: Callable[..., dict[str, str]],
) -> None:
    # Intended: a broken settings file is reported once, wherever it would be ignored.
    write_raw(config_dir, MALFORMED["not_toml"])

    with pytest.raises(ConfigError):
        resolve_config(CliOptions(demo=True), make_env())
    with pytest.raises(ConfigError):
        resolve_config(CliOptions(data_dir=tmp_path / "chosen"), make_env())


@pytest.mark.parametrize("name", ["JOBPIPELINE_DATA_DIR", "JOBPIPELINE_CONFIG_DIR"])
def test_empty_folder_override_env_raises_config_error(
    name: str, platform_dirs: Path, make_env: Callable[..., dict[str, str]]
) -> None:
    env = {**make_env(), name: ""}

    with pytest.raises(ConfigError):
        resolve_config(CliOptions(), env)


def test_bootstrap_that_is_a_directory_raises_config_error(config_dir: Path) -> None:
    (config_dir / "config.toml").mkdir(parents=True)

    with pytest.raises(ConfigError):
        load_bootstrap(config_dir)


def test_config_error_is_a_plain_app_error_that_never_shows_file_contents(
    config_dir: Path,
) -> None:
    write_raw(config_dir, MALFORMED["not_toml"])

    with pytest.raises(ConfigError) as caught:
        load_bootstrap(config_dir)

    error = caught.value
    assert isinstance(error, AppError)
    assert error.user_message.strip() != ""
    assert error.user_message != AppError().user_message  # says something specific
    assert MARKER not in error.user_message
    assert str(config_dir) not in error.user_message
    assert "Traceback" not in error.user_message
    assert "tomllib" not in error.user_message
    # Log detail may name the problem but never carries the file's contents.
    assert MARKER not in str(error)


def test_config_error_message_is_fixed_whatever_the_detail() -> None:
    assert ConfigError(detail="x").user_message == ConfigError().user_message


# --- config folder override (ADR-024) ------------------------------------------------------


def test_config_dir_override_env(platform_dirs: Path, tmp_path: Path) -> None:
    override = tmp_path / "elsewhere"

    assert resolve_config_dir({"JOBPIPELINE_CONFIG_DIR": str(override)}) == override


def test_config_dir_defaults_to_the_platform_folder(platform_dirs: Path) -> None:
    assert resolve_config_dir({}) == default_config_dir(platform_dirs)


def test_config_dir_override_is_where_the_bootstrap_is_read(
    platform_dirs: Path, tmp_path: Path
) -> None:
    # A bootstrap in the platform folder must be ignored once the override is set.
    write_bootstrap(default_config_dir(platform_dirs), data_dir=tmp_path / "platform-boot")
    override = tmp_path / "override-cfg"
    write_bootstrap(override, data_dir=tmp_path / "override-boot")

    resolved = resolve_config(CliOptions(), {"JOBPIPELINE_CONFIG_DIR": str(override)})

    assert resolved.config_dir == override
    assert resolved.data_dir == tmp_path / "override-boot"


def test_platform_config_bootstrap_is_used_without_the_override(
    platform_dirs: Path, tmp_path: Path
) -> None:
    write_bootstrap(default_config_dir(platform_dirs), data_dir=tmp_path / "platform-boot")

    resolved = resolve_config(CliOptions(), {})

    assert resolved.config_dir == default_config_dir(platform_dirs)
    assert resolved.data_dir == tmp_path / "platform-boot"


# --- purity and shape (ADR-030) ------------------------------------------------------------


def test_resolution_does_not_read_the_process_environment(
    platform_dirs: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_env: Callable[..., dict[str, str]],
) -> None:
    monkeypatch.setenv("JOBPIPELINE_PORT", "9999")
    monkeypatch.setenv("JOBPIPELINE_DATA_DIR", str(tmp_path / "process-env"))
    monkeypatch.setenv("JOBPIPELINE_LOG_LEVEL", "ERROR")

    resolved = resolve_config(CliOptions(), make_env())

    assert resolved.port == 8000
    assert resolved.log_level == "INFO"
    assert resolved.data_dir == default_data_dir(platform_dirs)


def test_resolution_does_not_modify_the_given_environment(
    platform_dirs: Path, make_env: Callable[..., dict[str, str]]
) -> None:
    env: Mapping[str, str] = make_env(JOBPIPELINE_PORT="9005")
    before = dict(env)

    resolve_config(CliOptions(), env)

    assert dict(env) == before


@pytest.mark.parametrize("cls", [BootstrapConfig, CliOptions, RuntimeConfig])
def test_config_dataclasses_are_frozen(cls: type) -> None:
    assert dataclasses.is_dataclass(cls)
    assert cls.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_cli_options_default_to_unset() -> None:
    cli = CliOptions()

    assert cli == CliOptions(
        data_dir=None, port=None, log_level=None, demo=False, setup=False, no_browser=False
    )
