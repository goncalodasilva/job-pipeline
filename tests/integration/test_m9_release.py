"""M9.6 parts C and D: what a released copy needs (ADR-131 item 9; TD-118; spec section 5 M9).

- The default prompts resolve in both layouts: inside the installed package (``app/prompts``,
  where the wheel puts them) and in a source checkout (the repository's ``prompts`` folder).
  ``scripts/check_wheel.py`` proves the installed case on the real wheel in ``verify.sh full``.
- The README lists every command-line option ``jobpipeline --help`` shows, and no other.
"""

import re
from pathlib import Path

import pytest

from app.cli import build_parser

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def test_prompts_dir_resolves_in_installed_layout(tmp_path: Path) -> None:
    from app.wiring import PROMPTS_DIR, prompts_dir

    installed = tmp_path / "site-packages" / "app"
    (installed / "prompts" / "summarize_job").mkdir(parents=True)
    assert prompts_dir(installed) == installed / "prompts"

    checkout = tmp_path / "checkout"
    (checkout / "app").mkdir(parents=True)
    (checkout / "prompts" / "summarize_job").mkdir(parents=True)
    assert prompts_dir(checkout / "app") == checkout / "prompts"

    # This checkout is the source layout, and its prompts are the ones the app loads.
    assert PROMPTS_DIR == ROOT / "prompts"
    assert (PROMPTS_DIR / "summarize_job" / "prompt.md").is_file()


def cli_options() -> set[str]:
    return {
        option
        for action in build_parser()._actions
        for option in action.option_strings
        if option.startswith("--")
    }


def test_readme_options_match_cli_help() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    help_text = build_parser().format_help()
    options = cli_options()
    assert options >= {"--port", "--data-dir", "--no-browser", "--setup", "--demo", "--version"}
    for option in options:
        assert option in help_text
        assert re.search(rf"`{re.escape(option)}\b", readme), f"README lacks {option}"
    # An option row in the README that the command does not have would mislead a newcomer.
    table_options = set(re.findall(r"^\| `(--[a-z-]+)", readme, flags=re.MULTILINE))
    assert table_options, "README has an options table"
    assert table_options <= options, table_options - options
    # And every option of the command has its own row, the help row included.
    assert options - {"--help"} <= table_options, (options - {"--help"}) - table_options
    assert re.search(r"^\| `-h`, `--help` \|", readme, flags=re.MULTILINE), "README help row"
