"""Mixed sessions: a plain ``pytest`` over the whole tree passes (M2.3, spec 4.1).

Playwright's sync API leaves an event loop marked as running on the main thread for as long as
its session fixture lives, so ``asyncio.run`` fails in any test that runs after an e2e test in
the same session (M1 known issue). The repository's ``tests/conftest.py`` orders e2e and smoke
items after every other item, stable within each group, so fast tests never run under that
loop.

Each test runs an inner pytest session whose ``pyproject.toml`` and root conftest are copies
of the repository's real files. The copied conftest sits in a folder not named ``tests``: with
``--import-mode=importlib`` an inner ``tests/conftest.py`` would resolve to the outer session's
already imported ``tests.conftest``, so the copy itself would never run. The inner "browser" is a
session fixture that marks a loop as running on the main thread, as Playwright does, and clears
it when the inner session ends, so the outer session is never affected.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FAILURES_DIR = REPO_ROOT / "tests" / "integration" / "failures"
INNER_ROOT = "suite_with_repo_conftest"
RUNNING_LOOP_MESSAGE = "cannot be called from a running event loop"

BROWSER_LIKE_TEST = """
import asyncio
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def browser_like_loop() -> Iterator[asyncio.AbstractEventLoop]:
    # Playwright's sync API leaves its loop marked as running on the main thread until the
    # session fixture is torn down; this does the same without a browser.
    loop = asyncio.new_event_loop()
    asyncio.events._set_running_loop(loop)
    try:
        yield loop
    finally:
        asyncio.events._set_running_loop(None)
        loop.close()


@pytest.mark.e2e
def test_browser_like(browser_like_loop: asyncio.AbstractEventLoop) -> None:
    assert asyncio.get_running_loop() is browser_like_loop
"""

ASYNCIO_RUN_TEST = """
import asyncio


async def _answer() -> int:
    return 42


def test_calls_asyncio_run() -> None:
    coro = _answer()
    try:
        assert asyncio.run(coro) == 42
    finally:
        coro.close()  # when asyncio.run refuses it, no "never awaited" warning leaks out
"""


def _use_real_project_config(pytester: pytest.Pytester) -> None:
    """Copy the repository's pyproject.toml and tests/conftest.py into the inner session.

    The conftest goes to ``INNER_ROOT``, so the inner session imports and runs the copy."""
    pytester.makepyprojecttoml((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    inner_dir = pytester.mkdir(INNER_ROOT)
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    (inner_dir / "conftest.py").write_text(conftest, encoding="utf-8")


def _write_test(
    pytester: pytest.Pytester, relative: str, source: str, root: str = INNER_ROOT
) -> None:
    target = pytester.path / root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def _write_loop_scenario(pytester: pytest.Pytester, root: str = INNER_ROOT) -> None:
    # "e2e" sorts before "unit", so collection puts the loop-leaving test first. The fixture
    # lives in the test module, so the scenario works under any root, with or without conftest.
    _write_test(pytester, "e2e/test_browser_like.py", BROWSER_LIKE_TEST, root)
    _write_test(pytester, "unit/test_uses_asyncio_run.py", ASYNCIO_RUN_TEST, root)


def _call_order(recorder: pytest.HookRecorder) -> list[str]:
    return [
        report.nodeid
        for report in recorder.getreports("pytest_runtest_logreport")
        if report.when == "call"
    ]


def test_asyncio_run_works_after_a_test_that_leaves_a_loop_running(
    pytester: pytest.Pytester,
) -> None:
    _use_real_project_config(pytester)
    _write_loop_scenario(pytester)

    recorder = pytester.inline_run(INNER_ROOT)

    passed, skipped, failed = recorder.listoutcomes()
    assert [report.nodeid for report in failed] == [], "asyncio.run failed in a mixed session"
    assert (len(passed), len(skipped)) == (2, 0)
    assert _call_order(recorder) == [
        f"{INNER_ROOT}/unit/test_uses_asyncio_run.py::test_calls_asyncio_run",
        f"{INNER_ROOT}/e2e/test_browser_like.py::test_browser_like",
    ]


def test_asyncio_run_fails_after_loop_left_running_without_repo_ordering(
    pytester: pytest.Pytester,
) -> None:
    """Control: the simulation reproduces the known issue, so the conftest is what fixes it.

    The same files under a folder with no conftest keep their collection order, and
    asyncio.run fails exactly as it did in M1."""
    pytester.makepyprojecttoml((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    _write_loop_scenario(pytester, root="suite")

    recorder = pytester.inline_run("suite")

    passed, _skipped, failed = recorder.listoutcomes()
    assert [report.nodeid for report in passed] == [
        "suite/e2e/test_browser_like.py::test_browser_like"
    ]
    assert [report.nodeid for report in failed] == [
        "suite/unit/test_uses_asyncio_run.py::test_calls_asyncio_run"
    ]
    assert RUNNING_LOOP_MESSAGE in str(failed[0].longrepr)


PLAIN_TEST_TEMPLATE = """
import pytest

{marker}
def test_{name}() -> None:
    pass
"""


def _plain_test(name: str, marker: str = "") -> str:
    decorator = f"@pytest.mark.{marker}" if marker else ""
    return PLAIN_TEST_TEMPLATE.format(marker=decorator, name=name)


MIXED_UNIT_FILE = """
import pytest


def test_unit_first() -> None:
    pass


@pytest.mark.e2e
def test_e2e_inside_unit_file() -> None:
    pass


def test_unit_last() -> None:
    pass
"""


def test_e2e_items_run_after_fast_tests(pytester: pytest.Pytester) -> None:
    _use_real_project_config(pytester)
    # Directories and files sort alphabetically, so e2e and smoke items are spread through
    # the collected session, including an e2e test between two unit tests in one file.
    _write_test(pytester, "contract/test_a_contract.py", _plain_test("contract", "contract"))
    _write_test(pytester, "e2e/test_b_page.py", _plain_test("page", "e2e"))
    _write_test(pytester, "e2e/test_c_smoke.py", _plain_test("smoke_one", "smoke"))
    _write_test(pytester, "integration/test_d_routes.py", _plain_test("routes", "integration"))
    _write_test(pytester, "integration/test_e_smoke.py", _plain_test("smoke_two", "smoke"))
    _write_test(pytester, "unit/test_f_domain.py", _plain_test("domain"))
    _write_test(pytester, "unit/test_g_mixed.py", MIXED_UNIT_FILE)
    _write_test(pytester, "unit/test_h_more_e2e.py", _plain_test("more_e2e", "e2e"))

    recorder = pytester.inline_run(INNER_ROOT)

    recorder.assertoutcome(passed=10)
    order = _call_order(recorder)
    fast = [
        f"{INNER_ROOT}/contract/test_a_contract.py::test_contract",
        f"{INNER_ROOT}/integration/test_d_routes.py::test_routes",
        f"{INNER_ROOT}/unit/test_f_domain.py::test_domain",
        f"{INNER_ROOT}/unit/test_g_mixed.py::test_unit_first",
        f"{INNER_ROOT}/unit/test_g_mixed.py::test_unit_last",
    ]
    e2e = [
        f"{INNER_ROOT}/e2e/test_b_page.py::test_page",
        f"{INNER_ROOT}/unit/test_g_mixed.py::test_e2e_inside_unit_file",
        f"{INNER_ROOT}/unit/test_h_more_e2e.py::test_more_e2e",
    ]
    smoke = [
        f"{INNER_ROOT}/e2e/test_c_smoke.py::test_smoke_one",
        f"{INNER_ROOT}/integration/test_e_smoke.py::test_smoke_two",
    ]
    assert order[: len(fast)] == fast, "fast tests must run first, in collection order"
    tail = order[len(fast) :]
    assert sorted(tail) == sorted(e2e + smoke)
    assert [nodeid for nodeid in tail if nodeid in e2e] == e2e, "e2e keep their relative order"
    assert [nodeid for nodeid in tail if nodeid in smoke] == smoke, "smoke keep relative order"


def test_failures_package_exists_and_holds_only_integration_tests() -> None:
    """``tests/integration/failures/`` exists (spec 4.1) and anything in it is ``integration``."""
    assert FAILURES_DIR.is_dir(), "tests/integration/failures/ is missing"
    unmarked = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and pytest
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            "not integration",
            str(FAILURES_DIR),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    # Exit code 5: nothing collected, i.e. no failure-injection test without the marker.
    assert unmarked.returncode == pytest.ExitCode.NO_TESTS_COLLECTED, unmarked.stdout[-2000:]


def test_failures_conftest_marks_unmarked_tests_as_integration(pytester: pytest.Pytester) -> None:
    """The guard above can fail: a test without ``pytestmark`` there still gets the marker.

    The conftest is a copy of the repository's, under ``INNER_ROOT`` so the copy is imported."""
    pytester.makepyprojecttoml((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    conftest = (FAILURES_DIR / "conftest.py").read_text(encoding="utf-8")
    _write_test(pytester, "integration/failures/conftest.py", conftest)
    _write_test(pytester, "integration/failures/test_unmarked.py", _plain_test("unmarked"))
    _write_test(pytester, "unit/test_elsewhere.py", _plain_test("elsewhere"))

    selected = pytester.inline_run(INNER_ROOT, "-m", "integration")
    deselected = pytester.inline_run(INNER_ROOT, "-m", "not integration")

    assert _call_order(selected) == [
        f"{INNER_ROOT}/integration/failures/test_unmarked.py::test_unmarked"
    ]
    assert _call_order(deselected) == [f"{INNER_ROOT}/unit/test_elsewhere.py::test_elsewhere"]
