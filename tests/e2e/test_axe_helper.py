"""The axe accessibility helper used by every UI scenario (M0.4, spec 3.9 and 4.4).

Each test loads small inline HTML with ``page.set_content``: no app, no network.
"""

import pytest
from playwright.sync_api import Page

from tests.e2e.axe import assert_no_serious_violations, run_axe

pytestmark = pytest.mark.e2e

BLOCKING_IMPACTS = {"serious", "critical"}

# 1x1 transparent PNG, so the image needs no network.
PIXEL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

BROKEN_PAGE = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Broken page</title></head>
<body>
  <main>
    <h1>Fictional job board</h1>
    <img src="{PIXEL}" width="40" height="40">
    <p style="color:#777777;background-color:#888888">Barely readable text</p>
  </main>
</body>
</html>"""

CLEAN_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Clean page</title>
</head>
<body style="color:#111111;background-color:#ffffff">
  <main>
    <h1>Fictional job search</h1>
    <form>
      <label for="keywords">Keywords</label>
      <input id="keywords" name="keywords" type="text">
      <button type="submit">Search</button>
    </form>
  </main>
</body>
</html>"""

# No <main> landmark and no h1: only moderate rules (landmark-one-main, region,
# page-has-heading-one) should fire.
MODERATE_ONLY_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Landmark-less page</title>
</head>
<body style="color:#111111;background-color:#ffffff">
  <p>Some fictional text outside any landmark.</p>
</body>
</html>"""


def test_axe_helper_flags_image_without_alt_and_low_contrast(desktop_page: Page) -> None:
    desktop_page.set_content(BROKEN_PAGE)

    with pytest.raises(AssertionError) as excinfo:
        assert_no_serious_violations(desktop_page)

    message = str(excinfo.value)
    assert "image-alt" in message
    assert "color-contrast" in message


def test_axe_helper_reports_violation_shape(desktop_page: Page) -> None:
    desktop_page.set_content(BROKEN_PAGE)

    violations = run_axe(desktop_page)

    ids = {violation["id"] for violation in violations}
    assert {"image-alt", "color-contrast"} <= ids
    for violation in violations:
        assert violation["impact"] in {"minor", "moderate", "serious", "critical"}
        assert violation["nodes"], f"{violation['id']} has no nodes"


@pytest.mark.parametrize("page_fixture", ["desktop_page", "phone_page"])
def test_axe_helper_passes_clean_page(page_fixture: str, request: pytest.FixtureRequest) -> None:
    page: Page = request.getfixturevalue(page_fixture)
    page.set_content(CLEAN_PAGE)

    assert_no_serious_violations(page)


def test_axe_helper_ignores_minor_and_moderate_violations(desktop_page: Page) -> None:
    desktop_page.set_content(MODERATE_ONLY_PAGE)

    violations = run_axe(desktop_page)

    assert violations, "expected at least one moderate or minor violation"
    assert all(v["impact"] in {"minor", "moderate"} for v in violations), violations
    assert_no_serious_violations(desktop_page)


# One serious violation from each non-core tag set, so dropping a tag from the helper fails.
TAG_CASES = {
    "best-practice": ("tabindex", '<a href="#x" tabindex="2">Skip</a>'),
    "wcag22aa": (
        "target-size",
        '<button style="width:10px;height:10px;padding:0;margin:0">a</button>'
        '<button style="width:10px;height:10px;padding:0;margin:0">b</button>',
    ),
}


@pytest.mark.parametrize("tag", sorted(TAG_CASES))
def test_axe_helper_runs_best_practice_and_wcag22_rules(tag: str, desktop_page: Page) -> None:
    rule, snippet = TAG_CASES[tag]
    desktop_page.set_content(
        '<!doctype html><html lang="en"><head><title>Tag case</title></head>'
        f"<body><main><h1>Tag case</h1>{snippet}</main></body></html>"
    )

    with pytest.raises(AssertionError, match=rule):
        assert_no_serious_violations(desktop_page)
