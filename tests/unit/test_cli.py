"""The ``jobpipeline --version`` command (M0.1, spec 7.1)."""

import importlib.metadata

import pytest


def test_cli_version_prints_package_version_and_exits_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.cli import main

    expected_version = importlib.metadata.version("jobpipeline")

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert f"jobpipeline {expected_version}" in capsys.readouterr().out


def test_cli_console_script_points_to_main() -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    matches = [ep for ep in scripts if ep.name == "jobpipeline"]

    assert [ep.value for ep in matches] == ["app.cli:main"]
