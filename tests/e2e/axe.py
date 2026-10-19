"""Accessibility checks with the vendored axe-core (spec 3.9, 4.4).

Every UI scenario calls ``assert_no_serious_violations(page)``: serious and critical
violations fail the test. ``run_axe`` returns every violation, including minor and moderate ones.
"""

from functools import cache
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

AXE_SCRIPT = Path(__file__).parent / "vendor" / "axe-core" / "axe.min.js"
RUN_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]
BLOCKING_IMPACTS = frozenset({"serious", "critical"})


@cache
def _axe_source() -> str:
    return AXE_SCRIPT.read_text(encoding="utf-8")


def run_axe(page: Page) -> list[dict[str, Any]]:
    """Run axe on the current page and return its violations."""
    if not page.evaluate("() => typeof window.axe !== 'undefined'"):
        page.add_script_tag(content=_axe_source())
    results: dict[str, Any] = page.evaluate(
        "async (tags) => await window.axe.run(document, "
        "{ runOnly: { type: 'tag', values: tags }, resultTypes: ['violations'] })",
        RUN_TAGS,
    )
    violations: list[dict[str, Any]] = results["violations"]
    return violations


def assert_no_serious_violations(page: Page) -> None:
    """Fail with each rule id and its targets when axe finds serious or critical problems."""
    blocking = [v for v in run_axe(page) if v.get("impact") in BLOCKING_IMPACTS]
    if not blocking:
        return
    lines = []
    for violation in blocking:
        nodes = violation.get("nodes", [])
        targets = "; ".join(" ".join(map(str, node.get("target", []))) for node in nodes[:5])
        if len(nodes) > 5:
            targets += f"; and {len(nodes) - 5} more"
        lines.append(
            f"{violation['id']} ({violation['impact']}): {violation['help']} at {targets}"
            f" ({violation.get('helpUrl', '')})"
        )
    raise AssertionError("Accessibility violations:\n" + "\n".join(lines))


SETTLED_STYLES = "() => document.getAnimations().every((a) => a.playState !== 'running')"


def wait_for_settled_styles(page: Page) -> None:
    """Wait until no CSS transition or animation is running, for a scan right after a change
    that starts one (such as switching to dark mid-page): axe must see the final colours."""
    page.wait_for_function(SETTLED_STYLES)
