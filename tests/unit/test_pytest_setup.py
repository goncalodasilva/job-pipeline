"""Pytest markers and live-test gating, checked against the real config (M0.1, spec 7.2).

Each test runs an inner pytest session whose ``pyproject.toml`` and
``tests/conftest.py`` are copies of the repository's real files, so these
tests exercise the project's actual configuration rather than a reimplementation.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_MARKERS = ["integration", "contract", "e2e", "smoke", "live", "slow"]
LIVE_ENV = "JOBPIPELINE_LIVE_TESTS"

LIVE_TEST_SOURCE = """
import pytest


@pytest.mark.live
def test_talks_to_real_provider() -> None:
    assert True
"""


def _use_real_project_config(pytester: pytest.Pytester) -> None:
    """Copy the repository's pyproject.toml and tests/conftest.py into the inner session."""
    pytester.makepyprojecttoml((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tests_dir = pytester.mkdir("tests")
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    (tests_dir / "conftest.py").write_text(conftest, encoding="utf-8")


def _write_test(pytester: pytest.Pytester, relative: str, source: str) -> None:
    target = pytester.path / "tests" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def test_markers_registered_and_strict(pytester: pytest.Pytester) -> None:
    _use_real_project_config(pytester)
    known = "\n".join(
        f"@pytest.mark.{name}\ndef test_marked_{name}() -> None:\n    pass\n\n"
        for name in SPEC_MARKERS
        if name != "live"
    )
    _write_test(pytester, "unit/test_known_markers.py", f"import pytest\n\n\n{known}")

    known_result = pytester.runpytest("tests/unit/test_known_markers.py")

    known_result.assert_outcomes(passed=len(SPEC_MARKERS) - 1)
    config_lines = pytester.runpytest("--markers").stdout.str()
    for name in SPEC_MARKERS:
        assert f"@pytest.mark.{name}:" in config_lines, f"marker {name!r} is not registered"

    _write_test(
        pytester,
        "unit/test_unknown_marker.py",
        "import pytest\n\n\n@pytest.mark.not_a_project_marker\ndef test_x() -> None:\n    pass\n",
    )

    unknown_result = pytester.runpytest("tests/unit/test_unknown_marker.py")

    assert unknown_result.ret != pytest.ExitCode.OK
    unknown_result.assert_outcomes(errors=1)
    unknown_result.stdout.fnmatch_lines(["*'not_a_project_marker' not found in `markers`*"])


def test_live_tests_skipped_without_env(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LIVE_ENV, raising=False)
    _use_real_project_config(pytester)
    _write_test(pytester, "unit/test_live_provider.py", LIVE_TEST_SOURCE)

    recorder = pytester.inline_run()

    passed, skipped, failed = recorder.listoutcomes()
    assert (len(passed), len(skipped), len(failed)) == (0, 1, 0), (
        "a live test must be skipped when JOBPIPELINE_LIVE_TESTS is unset"
    )
    longrepr = skipped[0].longrepr
    assert isinstance(longrepr, tuple)
    assert LIVE_ENV in longrepr[2]


def test_live_tests_run_with_env(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LIVE_ENV, "1")
    _use_real_project_config(pytester)
    _write_test(pytester, "unit/test_live_provider.py", LIVE_TEST_SOURCE)

    recorder = pytester.inline_run()

    passed, skipped, failed = recorder.listoutcomes()
    assert (len(passed), len(skipped), len(failed)) == (1, 0, 0)


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
def test_live_tests_skipped_when_env_is_not_1(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(LIVE_ENV, value)
    _use_real_project_config(pytester)
    _write_test(pytester, "unit/test_live_provider.py", LIVE_TEST_SOURCE)

    recorder = pytester.inline_run()

    passed, skipped, failed = recorder.listoutcomes()
    assert (len(passed), len(skipped), len(failed)) == (0, 1, 0), (
        f"a live test must be skipped when {LIVE_ENV}={value!r}"
    )


UNMARKED_LIVE_NAMED_SOURCE = """
import pytest


@pytest.mark.parametrize("mode", ["live"])
def test_unmarked_with_live_param_id(mode: str) -> None:
    assert mode == "live"
"""

UNMARKED_SOURCE = """
def test_unmarked_in_live_folder() -> None:
    assert True
"""


def test_unmarked_tests_named_live_are_not_skipped(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LIVE_ENV, raising=False)
    _use_real_project_config(pytester)
    _write_test(pytester, "unit/test_live_param_id.py", UNMARKED_LIVE_NAMED_SOURCE)
    _write_test(pytester, "live/test_unmarked.py", UNMARKED_SOURCE)

    recorder = pytester.inline_run()

    passed, skipped, failed = recorder.listoutcomes()
    assert (len(passed), len(skipped), len(failed)) == (2, 0, 0), (
        "only tests carrying the live marker may be skipped"
    )
