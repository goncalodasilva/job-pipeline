"""The subprocess harness's demo mode (M2.26 review): ``start_app(..., demo=True)`` starts
``--demo`` the way a user does, on the app's own demo folder under the temporary home.

These are the refusals and the folder it computes; no app is started here (the e2e tests
``test_m2_demo.py`` and ``test_demo_guide.py`` start it). The only child process is the
harness's own one-line ``platformdirs`` question. Every folder is under ``tmp_path``.
"""

from pathlib import Path

import pytest

from app.config import DEMO_APP_NAME, ENV_DATA_DIR
from tests.running_app import (
    ALLOWED_CLI_ARGS,
    DEMO_FLAG,
    isolated_env,
    platform_default_data_dir,
    start_app,
)

ALLOW_FAKES = "JOBPIPELINE_ALLOW_FAKE_PROVIDERS"


def demo_env(tmp_path: Path) -> dict[str, str]:
    return isolated_env(
        tmp_path / "data", tmp_path / "config", allow_fakes=False, explicit_data_dir=False
    )


def test_demo_flag_cannot_be_passed_as_a_plain_flag(tmp_path: Path) -> None:
    """``--demo`` with ``JOBPIPELINE_DATA_DIR`` set is not the demo a user runs: the harness
    only starts a demo through ``demo=True``."""
    assert DEMO_FLAG not in ALLOWED_CLI_ARGS

    with pytest.raises(ValueError, match="unsupported app flags"):
        start_app(tmp_path, cli_args=(DEMO_FLAG,))

    assert not (tmp_path / "data").exists(), "refused before anything was written"


@pytest.mark.parametrize("setup", ["first_run", "provider_step", "preferences_step"])
def test_a_demo_starts_complete_or_fresh_only(tmp_path: Path, setup: str) -> None:
    with pytest.raises(ValueError, match="a demo starts"):
        start_app(tmp_path, demo=True, setup=setup, allow_fakes=False)  # type: ignore[arg-type]

    assert list(tmp_path.iterdir()) == [], "refused before anything was written"


def test_demo_environment_names_no_data_folder_and_no_allow_variable(tmp_path: Path) -> None:
    env = demo_env(tmp_path)

    assert ENV_DATA_DIR not in env
    assert ALLOW_FAKES not in env
    assert env["HOME"] == str(tmp_path / "home")
    assert Path(env["JOBPIPELINE_CONFIG_DIR"]) == tmp_path / "config"


def test_demo_folder_is_the_apps_own_under_the_temporary_home(tmp_path: Path) -> None:
    env = demo_env(tmp_path)

    folder = platform_default_data_dir(env, tmp_path, app_name=DEMO_APP_NAME)
    plain = platform_default_data_dir(env, tmp_path)

    assert folder.resolve().is_relative_to((tmp_path / "home").resolve())
    assert DEMO_APP_NAME in folder.parts
    assert folder != plain, "the demo never shares the default folder"
    assert not folder.exists(), "asking where it is creates nothing"


def test_demo_restart_needs_the_demo_folder_of_an_earlier_start(tmp_path: Path) -> None:
    # An explicit run's folders exist, but no demo ran here.
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()

    with pytest.raises(ValueError, match="restart needs"):
        start_app(tmp_path, demo=True, restart=True, allow_fakes=False)
